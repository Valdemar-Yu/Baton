import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class StorageRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-retention-")
        self.base = Path(self.tmp.name)
        self.project_root = self.base / "project"
        self.project_root.mkdir()
        self.home = self.base / "home"
        self.codex = self.base / "codex"
        self.hermes = self.base / "hermes"
        self.state = self.base / "baton-home"
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.state), "CODEX_HOME": str(self.codex),
            "HERMES_HOME": str(self.hermes), "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        (self.project_root / ".baton").mkdir()
        (self.project_root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        (self.project_root / "baton.json").write_text(json.dumps({
            "executor": {"backend": "codex"}, "memory": {"backend": "local"},
        }), encoding="utf-8")
        self.project = baton.Project(str(self.project_root))
        self.project.record_id()
        self.make_task("t", "idle")

    def make_task(self, task, status="idle", **extra):
        task_dir = Path(self.project.task_dir(task))
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "brief.md").write_text("# " + task + "\n", encoding="utf-8")
        state = {"task": task, "status": status, "legs": [], "scope": {
            "allow": ["*"], "deny": [], "acceptance": [], "frozen": [], "verify": "none",
        }}
        state.update(extra)
        self.project.update_state(task, lambda current: current.update(state))

    def make_dossier(self, ident, task="t", kind="report", created=None, code=True):
        root = Path(baton.dossier_path(self.project, ident))
        root.mkdir(parents=True, exist_ok=True)
        if code:
            (root / "code").mkdir(exist_ok=True)
            (root / "code" / "payload.txt").write_text(ident + "\n", encoding="utf-8")
        (root / "report.html").write_text("report\n", encoding="utf-8")
        files = baton._dossier_files(str(root))
        baton.write_json(str(root / "manifest.json"), {
            "dossier": ident, "task": task, "type": kind, "created_at": created or baton.now(),
            "files": files, "total_sha256": baton._dossier_total(files),
        })
        baton._seal_dossier(str(root))
        return root

    def test_codex_shared_dirs_and_instruction_exclusion(self):
        for name in ("cache", "plugins", "packages", "sessions"):
            (self.codex / name).mkdir(parents=True)
        (self.codex / "cache" / "shared.bin").write_bytes(b"x")
        (self.codex / "AGENTS.md").write_text("global rules\n", encoding="utf-8")
        (self.codex / "AGENTS.override.md").write_text("override\n", encoding="utf-8")
        target, inherited, excluded = baton.prepare_task_codex_home(self.project, "t")
        target = Path(target)
        self.assertTrue((target / "cache").is_symlink())
        self.assertTrue((target / "plugins").is_symlink())
        self.assertTrue((target / "packages").is_symlink())
        self.assertFalse((target / "sessions").exists())
        self.assertFalse((target / "AGENTS.md").exists())
        self.assertEqual(set(excluded), {"AGENTS.md", "AGENTS.override.md"})
        self.assertEqual(set(inherited), {"cache", "packages", "plugins"})

    def test_hermes_mutable_dirs_stay_local_and_auth_is_shared(self):
        profile = self.hermes / "profiles" / "baton-exec-v2"
        profile.mkdir(parents=True)
        (profile / "auth.json").write_text("{}", encoding="utf-8")
        for name in ("bin", "skills", "cache"):
            (profile / name).mkdir()
        cfg = self.project.config
        cfg["executor"] = dict(cfg["executor"], backend="hermes")
        with patch.object(baton, "_scan_soul_with_hermes", return_value=[]):
            baton.prepare_task_hermes_profile(self.project, "t", cfg)
        task_profile = Path(baton.hermes_profile_dir(cfg, project=self.project, task="t"))
        self.assertFalse((task_profile / "bin").is_symlink())
        self.assertFalse((task_profile / "skills").is_symlink())
        self.assertFalse((task_profile / "cache").exists())
        self.assertTrue((task_profile / "auth.json").is_symlink())
        self.assertTrue((task_profile / "SOUL.md").is_file())
        self.assertTrue((task_profile / "config.yaml").is_file())

    def test_new_report_prunes_replaced_code_and_keeps_latest(self):
        old = self.make_dossier("R001", created=1)
        new = self.make_dossier("R002", created=2)
        baton._prune_superseded_report_dossiers(self.project, "t", "R002")
        self.assertFalse((old / "code").exists())
        self.assertTrue((new / "code").exists())
        self.assertIn("R002", baton.dossier_manifest(self.project, "R001")["code_pruned"]["reason"])
        self.assertEqual(baton.verify_dossier(self.project, "R001"), (True, ""))

    def test_named_final_prunes_earlier_violation(self):
        violation = self.make_dossier("t-leg-001-violation", kind="violation", created=1)
        final = self.make_dossier("t-final", kind="final", created=2)
        baton._prune_superseded_report_dossiers(self.project, "t", "t-final")
        self.assertFalse((violation / "code").exists())
        self.assertTrue((final / "code").exists())

    def test_report_numbers_and_final_sequence_are_not_limited_to_three_digits(self):
        old_report = self.make_dossier("R999", created=1)
        current_report = self.make_dossier("R1000", created=2)
        baton._prune_superseded_report_dossiers(self.project, "t", "R1000")
        self.assertFalse((old_report / "code").exists())
        self.assertTrue((current_report / "code").exists())
        old_final = self.make_dossier("t-final-1", kind="final", created=3)
        current_final = self.make_dossier("t-final-2", kind="final", created=4)
        baton._prune_superseded_report_dossiers(self.project, "t", "t-final-2")
        self.assertFalse((old_final / "code").exists())
        self.assertTrue((current_final / "code").exists())

    def test_revision_count_accepts_four_digit_report_ids(self):
        original = self.make_dossier("R1000", created=1)
        revision = self.make_dossier("R1001", created=2)
        baton._unseal_dossier(str(original))
        baton._unseal_dossier(str(revision))
        (original / "report.html").write_text("原始汇报", encoding="utf-8")
        (revision / "report.html").write_text("修订自 R1000", encoding="utf-8")
        baton._seal_dossier(str(original))
        baton._seal_dossier(str(revision))
        for dossier in ("R1000", "R1001"):
            baton.append_ledger(self.project, {
                "type": "verdict", "task": "t", "dossier": dossier,
                "conclusion": "REVISE", "findings": {}, "verdict": "v",
                "rendered": "v",
            })
        self.assertEqual(baton._revision_counts(self.project, "t")["R1000"], 2)

    def _make_closed_payload(self):
        dossier = self.make_dossier("t-final", kind="final")
        out = Path(baton.dossier_out_dir(self.project, "t-final"))
        out.mkdir(parents=True)
        (out / "attempt.events.jsonl").write_text("events\n", encoding="utf-8")
        (out / "attempt.stdout.json").write_text("stdout\n", encoding="utf-8")
        (out / "attempt.result.json").write_text("result\n", encoding="utf-8")
        (out / "attempt.stderr.log").write_text("stderr\n", encoding="utf-8")
        (out / "attempt.runner.log").write_text("runner\n", encoding="utf-8")
        frozen = Path(baton.freeze_dir(self.project, "t"))
        (frozen / "files").mkdir(parents=True)
        (frozen / "files" / "locked.py").write_text("locked\n", encoding="utf-8")
        (frozen / "manifest.json").write_text("{}\n", encoding="utf-8")
        codex_home = Path(baton.codex_task_home(self.project, "t"))
        (codex_home / "sessions").mkdir(parents=True)
        (codex_home / "sessions" / "keep.jsonl").write_text("keep\n", encoding="utf-8")
        (codex_home / "cache.bin").write_text("drop\n", encoding="utf-8")
        hermes_home = Path(baton.hermes_task_home(self.project, "t"))
        (hermes_home / "sessions").mkdir(parents=True)
        (hermes_home / "sessions" / "keep.jsonl").write_text("keep\n", encoding="utf-8")
        (hermes_home / "state.db").write_text("db\n", encoding="utf-8")
        (hermes_home / "skills.bin").write_text("drop\n", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(
            status="closed", codex_home=str(codex_home), hermes_home=str(hermes_home),
            legs=[{"n": 1, "dossier": {"id": "t-final"}}]))
        return dossier, out, frozen, codex_home, hermes_home

    def test_gc_dry_run_categories_then_cleanup_preserves_metadata(self):
        dossier, out, frozen, codex_home, hermes_home = self._make_closed_payload()
        external = self.base / "external"
        external.mkdir()
        (external / "keep.txt").write_text("external\n", encoding="utf-8")
        (codex_home / "shared").symlink_to(external, target_is_directory=True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=True, stale_days=14))
        text = output.getvalue()
        dry_stats = self._gc_category_stats(text, "将清理")
        self.assertEqual(
            dry_stats["total"],
            {
                "files": sum(dry_stats[name]["files"] for name in self._gc_categories),
                "bytes": sum(dry_stats[name]["bytes"] for name in self._gc_categories),
            },
        )
        self.assertTrue((out / "attempt.events.jsonl").exists())
        actual_output = io.StringIO()
        with contextlib.redirect_stdout(actual_output):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        actual_stats = self._gc_category_stats(actual_output.getvalue(), "已清理")
        for category in self._gc_categories + ("total",):
            self.assertEqual(actual_stats[category], dry_stats[category], category)
        self.assertFalse((dossier / "code").exists())
        self.assertFalse((out / "attempt.events.jsonl").exists())
        self.assertFalse((out / "attempt.stdout.json").exists())
        self.assertTrue((out / "attempt.result.json").exists())
        self.assertTrue((out / "attempt.stderr.log").exists())
        self.assertTrue((out / "attempt.runner.log").exists())
        self.assertTrue((frozen / "manifest.json").exists())
        self.assertFalse((frozen / "files" / "locked.py").exists())
        self.assertTrue((codex_home / "sessions" / "keep.jsonl").exists())
        self.assertTrue((hermes_home / "state.db").exists())
        self.assertTrue((hermes_home / "sessions" / "keep.jsonl").exists())
        self.assertTrue((external / "keep.txt").exists())
        self.assertTrue(baton.verify_dossier(self.project, "t-final")[0])

    _gc_categories = ("卷宗代码", "事件日志", "frozen 文件", "task home")

    def _gc_category_stats(self, output, prefix):
        stats = {}
        for line in output.splitlines():
            match = re.match(rf"^{re.escape(prefix)} (.+)：(\d+) 个文件，(\d+) 字节$", line)
            if match and match.group(1) in self._gc_categories:
                stats[match.group(1)] = {
                    "files": int(match.group(2)), "bytes": int(match.group(3)),
                }
            match = re.match(rf"^{re.escape(prefix)}合计：(\d+) 个文件，(\d+) 字节$", line)
            if match:
                stats["total"] = {"files": int(match.group(1)), "bytes": int(match.group(2))}
        self.assertEqual(set(stats), set(self._gc_categories) | {"total"})
        return stats

    def test_repeated_gc_without_new_files_does_not_rewrite_state(self):
        self._make_closed_payload()
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        state_path = Path(self.project.state_path("t"))
        first = state_path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        self.assertEqual(state_path.read_bytes(), first)

    def test_stale_days_prunes_old_reports_and_check_events_but_skips_running(self):
        old = self.make_dossier("R010", created=1)
        latest = self.make_dossier("R011", created=2)
        check = self.make_dossier("t-leg-001-c01", kind="check", created=1)
        out = Path(baton.dossier_out_dir(self.project, "t-leg-001-c01"))
        out.mkdir(parents=True)
        (out / "check.events.jsonl").write_text("old\n", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(status="idle", updated_at=baton.now() - 30 * 86400,
                                                                    legs=[{"n": 1, "status": "report"}]))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        self.assertFalse((old / "code").exists())
        self.assertTrue((latest / "code").exists())
        self.assertFalse((out / "check.events.jsonl").exists())

        running = self.base / "running"
        self.make_task("running", "running", updated_at=baton.now() - 30 * 86400,
                        legs=[{"n": 1, "status": "running"}])
        run_dossier = self.make_dossier("R020", task="running", created=1)
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        self.assertTrue((run_dossier / "code").exists())

    def test_close_abandon_runs_same_cleanup(self):
        dossier, out, frozen, codex_home, hermes_home = self._make_closed_payload()
        self.project.update_state("t", lambda state: state.update(status="idle"))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_close(self.project, argparse.Namespace(task="t", abandon="stop", waive=None,
                                                              user_approved=False))
        self.assertFalse((dossier / "code").exists())
        self.assertFalse((out / "attempt.events.jsonl").exists())
        self.assertFalse((frozen / "files" / "locked.py").exists())
        self.assertFalse((codex_home / "cache.bin").exists())
        self.assertTrue((codex_home / "sessions" / "keep.jsonl").exists())
        self.assertTrue((hermes_home / "state.db").exists())

    def test_symlink_containers_never_touch_external_files_and_count_once(self):
        dossier = self.make_dossier("t-final", kind="final")
        external = self.base / "outside"
        external.mkdir()
        (external / "attempt.events.jsonl").write_text("keep\n", encoding="utf-8")
        out = Path(baton.dossier_out_dir(self.project, "t-final"))
        out.symlink_to(external, target_is_directory=True)
        frozen = Path(baton.freeze_dir(self.project, "t"))
        frozen.mkdir(parents=True)
        (frozen / "manifest.json").write_text("{}\n", encoding="utf-8")
        external_files = self.base / "outside-files"
        external_files.mkdir()
        (external_files / "locked.py").write_text("keep\n", encoding="utf-8")
        (frozen / "files").symlink_to(external_files, target_is_directory=True)
        self.project.update_state("t", lambda state: state.update(status="closed", legs=[{"n": 1, "dossier": {"id": "t-final"}}]))
        preview = io.StringIO()
        with contextlib.redirect_stdout(preview):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=True, stale_days=14))
        self.assertTrue(out.is_symlink())
        self.assertTrue((frozen / "files").is_symlink())
        (Path(baton.codex_task_home(self.project, "t"))).mkdir(parents=True, exist_ok=True)
        home = Path(baton.codex_task_home(self.project, "t"))
        (home / "link.txt").symlink_to(external / "attempt.events.jsonl")
        (home / "link-dir").symlink_to(external, target_is_directory=True)
        stats = baton._cleanup_task_home(str(home), "codex", self.project.state_dir, dry_run=True)
        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["bytes"], os.lstat(home / "link.txt").st_size + os.lstat(home / "link-dir").st_size)
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False, stale_days=14))
        self.assertTrue((external / "attempt.events.jsonl").exists())
        self.assertTrue((external_files / "locked.py").exists())
        self.assertFalse(out.exists())
        self.assertFalse((frozen / "files").exists())

    def test_frozen_root_and_dossier_links_only_unlink_links(self):
        external_frozen = self.base / "external-frozen"
        external_frozen.mkdir()
        (external_frozen / "manifest.json").write_text("keep\n", encoding="utf-8")
        frozen_parent = Path(baton.freeze_dir(self.project, "t")).parent
        frozen_parent.mkdir(parents=True, exist_ok=True)
        frozen_root = Path(baton.freeze_dir(self.project, "t"))
        frozen_root.symlink_to(external_frozen, target_is_directory=True)
        stats = baton._cleanup_frozen_task(self.project, "t", dry_run=True)
        self.assertEqual(stats["files"], 1)
        baton._cleanup_frozen_task(self.project, "t", dry_run=False)
        self.assertFalse(frozen_root.exists())
        self.assertTrue((external_frozen / "manifest.json").exists())

        external_dossier = self.base / "external-dossier"
        (external_dossier / "code").mkdir(parents=True)
        (external_dossier / "code" / "keep.txt").write_text("keep\n", encoding="utf-8")
        dossier_link = Path(baton.dossier_path(self.project, "R090"))
        dossier_link.parent.mkdir(parents=True, exist_ok=True)
        dossier_link.symlink_to(external_dossier, target_is_directory=True)
        baton._prune_dossier_code(self.project, "R090", "link root", dry_run=False)
        self.assertFalse(dossier_link.exists())
        self.assertTrue((external_dossier / "code" / "keep.txt").exists())

        normal = self.make_dossier("R091", code=False)
        external_code = self.base / "external-code"
        external_code.mkdir()
        (external_code / "keep.txt").write_text("keep\n", encoding="utf-8")
        baton._unseal_dossier(str(normal))
        (normal / "code").symlink_to(external_code, target_is_directory=True)
        baton._seal_dossier(str(normal))
        baton._prune_dossier_code(self.project, "R091", "link code", dry_run=False)
        self.assertFalse((normal / "code").exists())
        self.assertTrue((external_code / "keep.txt").exists())

        with_code_link = self.make_dossier("R092")
        external_code_dir = self.base / "external-code-dir"
        external_code_dir.mkdir()
        (external_code_dir / "keep.txt").write_text("keep\n", encoding="utf-8")
        external_code_file = self.base / "external-code-file.txt"
        external_code_file.write_text("keep-file\n", encoding="utf-8")
        os.chmod(external_code_file, 0o601)
        external_file_mode = stat.S_IMODE(external_code_file.stat().st_mode)
        external_file_content = external_code_file.read_bytes()
        baton._unseal_dossier(str(with_code_link))
        (with_code_link / "code" / "external-dir").symlink_to(external_code_dir, target_is_directory=True)
        (with_code_link / "code" / "external-file").symlink_to(external_code_file)
        baton._seal_dossier(str(with_code_link))
        dry = baton._prune_dossier_code(self.project, "R092", "symlink directory", dry_run=True)
        actual = baton._prune_dossier_code(self.project, "R092", "symlink directory", dry_run=False)
        self.assertEqual(dry["files"], actual["files"])
        self.assertEqual(dry["bytes"], actual["bytes"])
        self.assertTrue((external_code_dir / "keep.txt").exists())
        self.assertEqual(external_code_file.read_bytes(), external_file_content)
        self.assertEqual(stat.S_IMODE(external_code_file.stat().st_mode), external_file_mode)

    def test_task_home_root_links_only_unlink_links(self):
        for kind, home_func in (("codex", baton.codex_task_home), ("hermes", baton.hermes_task_home)):
            with self.subTest(kind=kind):
                external = self.base / (kind + "-external")
                external.mkdir()
                (external / "keep.txt").write_text("keep\n", encoding="utf-8")
                home = Path(home_func(self.project, "root-link"))
                home.parent.mkdir(parents=True, exist_ok=True)
                home.symlink_to(external, target_is_directory=True)
                dry = baton._cleanup_task_home(str(home), kind, self.project.state_dir, dry_run=True)
                self.assertEqual(dry["files"], 1)
                baton._cleanup_task_home(str(home), kind, self.project.state_dir, dry_run=False)
                self.assertFalse(home.exists())
                self.assertTrue((external / "keep.txt").exists())

    def test_doctor_reports_state_size_threshold(self):
        state_dir = Path(self.project.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "small.bin").write_bytes(b"x")
        self.assertIn("项目外状态目录", self._doctor_output())
        (state_dir / "large.bin").write_bytes(b"x")
        with patch.object(baton, "_state_dir_size", return_value=2 * 1024 ** 3 + 1):
            output = self._doctor_output()
        self.assertIn("超过 2 GiB", output)
        self.assertIn("baton gc --dry-run", output)

    def _doctor_output(self):
        output = io.StringIO()
        patches = [
            patch.object(baton, "doctor_memory", return_value=False),
            patch.object(baton, "safe_quota", return_value=None),
            patch.object(baton.shutil, "which", return_value="/usr/bin/codex"),
            patch.object(baton, "run", return_value="codex 0"),
            patch.object(baton.codex_quota, "describe", return_value="quota"),
            patch.object(baton.codex_quota, "is_low", return_value=False),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(output):
                with self.assertRaises(SystemExit) as raised:
                    baton.cmd_doctor(self.project, argparse.Namespace(monitor_probe=False))
        self.assertEqual(raised.exception.code, 0)
        return output.getvalue()


if __name__ == "__main__":
    unittest.main()
