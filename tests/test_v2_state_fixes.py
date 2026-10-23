from platform_fixtures import executable_fixture
"""Regression tests for the issues found by the independent review of phase 1 (v2-state)."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import shutil
import tempfile
import textwrap
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BATON_PY = ROOT / "skills" / "baton" / "scripts" / "baton.py"
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in baton.GIT_LOCAL_ENV_VARS}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    env.update(extra)
    return env


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], env=clean_env(), check=True,
                          capture_output=True, text=True).stdout


def quiet(fn, *args, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args, **kw)
    return out.getvalue()


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(os.path.realpath(tmp.name))
        env = patch.dict(os.environ, {"HOME": str(self.tmp / "home"), "USERPROFILE": str(self.tmp / "home"), "BATON_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)

    def init(self, proj, **kw):
        path = Path(proj) / "baton.json"
        if not path.exists():
            path.write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        project = baton.Project(str(proj))
        out = quiet(baton.cmd_init, project, Namespace(no_statusline=True, accept_boundary=True, **kw))
        return project, out

    def start_task(self, project, task):
        project.update_state(task, lambda s: s.update(task=task, snapshot_store="shadow"))
        ref, err = baton.try_checkpoint(project, task, "base")
        self.assertIsNone(err)
        project.update_state(task, lambda s: s.update(base_ref=ref))
        return ref

    def tree(self, project, commit):
        return set(project.store().git("ls-tree", "-r", "--name-only", commit).split())


class NestedAndExcludeTests(Base):
    def test_outer_rollback_leaves_a_nested_project_alone(self):
        outer = self.tmp / "vault"
        write(outer / "baton.json", "{}")
        write(outer / "notes" / "a.md", "outer\n")
        write(outer / "Bica" / "baton.json", "{}")
        write(outer / "Bica" / "main.py", "inner = 1\n")
        project, _ = self.init(outer)
        inner, _ = self.init(outer / "Bica")
        base = self.start_task(project, "t1")
        write(outer / "notes" / "a.md", "outer changed\n")
        write(outer / "Bica" / "main.py", "inner = 2\n")
        write(outer / "Bica" / ".baton" / "log.md", (outer / "Bica" / ".baton" / "log.md").read_text() + "\nnew\n")
        write(outer / "Bica" / ".baton" / "reports" / "R001.html", "report\n")
        stat = quiet(baton.cmd_diff, project, Namespace(task="t1", since="base", stat=True))
        self.assertIn("notes/a.md", stat)
        self.assertNotIn(".baton", stat)
        out = quiet(baton.cmd_rollback, project, Namespace(task="t1", ref=base, yes=True))
        self.assertIn("属于嵌套项目 Bica", out)
        self.assertEqual((outer / "notes" / "a.md").read_text(), "outer\n")
        self.assertEqual((outer / "Bica" / "main.py").read_text(), "inner = 2\n")
        self.assertTrue((outer / "Bica" / ".baton" / "reports" / "R001.html").exists())
        self.assertIn("new", (outer / "Bica" / ".baton" / "log.md").read_text())
        self.assertIn("嵌套项目", quiet(baton.print_snapshot_scope, project))

    def test_settings_local_json_is_not_snapshotted(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        write(proj / ".claude" / "settings.local.json", '{"statusLine": {}}')
        project, _ = self.init(proj)
        commit = project.store().snapshot("probe")
        files = self.tree(project, commit)
        self.assertIn("a.py", files)
        self.assertNotIn(".claude/settings.local.json", files)

    def test_nested_settings_local_json_is_excluded_and_scope_marks_directory(self):
        proj = self.tmp / "p"
        write(proj / "src" / ".claude" / "settings.local.json", '{"statusLine": {}}')
        project, out = self.init(proj)
        files = self.tree(project, project.store().snapshot("probe"))
        self.assertNotIn("src/.claude/settings.local.json", files)
        self.assertIn("排除", out)

    def test_code_excludes_prunes_shadow_dirs_and_caches_scan(self):
        proj = self.tmp / "p"
        write(proj / "node_modules" / "pkg" / "baton.json", "{}")
        write(proj / "data" / "nested" / "baton.json", "{}")
        write(proj / "src" / "baton.json", "{}")
        project, _ = self.init(proj)
        baton._CODE_EXCLUDES_CACHE.pop(project.root, None)
        baton._CODE_NESTED_CACHE.pop(project.root, None)
        with patch.object(baton.os, "walk", wraps=os.walk) as walk:
            first = baton.code_excludes(project.store())
            second = baton.code_excludes(project.store())
        self.assertEqual(first, second)
        self.assertEqual(walk.call_count, 1)
        self.assertIn(":(exclude,glob)src/**", first)
        self.assertNotIn("node_modules/pkg", " ".join(first))
        self.assertNotIn("data/nested", " ".join(first))

    def test_scope_checks_directory_files_in_one_batch(self):
        proj = self.tmp / "p"
        for name in ("a.py", "b.py", "c.py"):
            write(proj / "src" / name, "x\n")
        project, _ = self.init(proj)
        calls = []
        real_run = baton.subprocess.run

        def wrapped(*args, **kwargs):
            command = args[0] if args else kwargs.get("args")
            if command and "check-ignore" in command:
                calls.append(kwargs.get("input", ""))
            return real_run(*args, **kwargs)

        with patch.object(baton.subprocess, "run", side_effect=wrapped):
            quiet(baton.print_snapshot_scope, project)
        src_calls = [payload for payload in calls if "src" in payload]
        self.assertEqual(len(src_calls), 1)
        self.assertGreaterEqual(src_calls[0].count("\0"), 4)

    def test_exclude_added_later_applies_to_snapshotted_files(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "a = 1\n")
        write(proj / "bulk" / "big.dat", "v1\n")
        project, _ = self.init(proj)
        base = self.start_task(project, "t1")
        self.assertIn("bulk/big.dat", self.tree(project, base))
        write(proj / "bulk" / "big.dat", "v2\n")
        exclude = Path(project.snap_git_dir) / "info" / "exclude"
        exclude.write_text(exclude.read_text() + "/bulk/\n", encoding="utf-8")
        stat = quiet(baton.cmd_diff, project, Namespace(task="t1", since="base", stat=True))
        self.assertIn("bulk/big.dat", stat)  # it was in base and is gone from the new snapshot
        self.assertIn("当前被影子仓库排除", stat)
        commit = project.store().snapshot("probe")
        self.assertNotIn("bulk/big.dat", self.tree(project, commit))
        project.update_state("t1", lambda s: s.update(
            status="idle", legs=[{"n": 1, "kind": "start", "status": "done", "started_at": time.time(),
                                  "start_ref": base, "events": str(proj / ".baton/runs/t1/leg-001.events.jsonl"),
                                  "log_size_at_start": 0}]))
        status = quiet(baton.cmd_status, project, Namespace(task="t1"))
        self.assertIn("当前被影子仓库排除", status)
        write(proj / "a.py", "a = 2\n")
        rollback = quiet(baton.cmd_rollback, project, Namespace(task="t1", ref=base, yes=True))
        self.assertEqual((proj / "a.py").read_text(), "a = 1\n")
        self.assertEqual((proj / "bulk" / "big.dat").read_text(), "v2\n")
        self.assertIn("bulk/big.dat（已排除）", rollback)

    def test_defaults_follow_decision_d001(self):
        proj = self.tmp / "ml"
        for rel in ("src/models/net.py", "src/data/loader.py", "exp/run1/checkpoints/e.pt",
                    "exp/run1/logs/train.log", "scripts/outputs/r.csv", "models/w.bin", "data/x.csv", "a.py"):
            write(proj / rel, "x\n")
        project, out = self.init(proj)
        files = self.tree(project, project.store().snapshot("probe"))
        self.assertTrue({"src/models/net.py", "src/data/loader.py", "a.py"} <= files)
        for gone in ("exp/run1/checkpoints/e.pt", "exp/run1/logs/train.log", "scripts/outputs/r.csv",
                     "models/w.bin", "data/x.csv"):
            self.assertNotIn(gone, files)
        self.assertIn("嵌套的 data/models/outputs/logs/checkpoints 目录", out)
        self.assertIn("exp/run1/checkpoints/", out)

    def test_rewritten_exclude_file_is_not_refilled(self):
        proj = self.tmp / "p"
        write(proj / "models" / "net.py", "x\n")
        project, _ = self.init(proj)
        exclude = Path(project.snap_git_dir) / "info" / "exclude"
        exclude.write_text("# my own rules\n/scratch/\n", encoding="utf-8")
        files = self.tree(project, project.store().snapshot("probe"))
        self.assertIn("models/net.py", files)
        self.assertEqual(exclude.read_text(), "# my own rules\n/scratch/\n")


class UserTrackedTests(Base):
    def test_init_scope_labels_user_tracked_ignored_file_as_snapshot(self):
        proj = self.tmp / "p"
        proj.mkdir()
        git(proj, "init", "-q")
        write(proj / ".gitignore", "*.cfg\n")
        write(proj / "app.cfg", "port=1\n")
        git(proj, "add", ".gitignore")
        git(proj, "add", "-f", "app.cfg")
        git(proj, "commit", "-q", "-m", "init")
        _, out = self.init(proj)
        self.assertIn("快照（用户仓库已跟踪）", out)

    def test_force_added_tracked_file_is_snapshotted_and_restored(self):
        proj = self.tmp / "p"
        proj.mkdir()
        git(proj, "init", "-q")
        write(proj / ".gitignore", "*.cfg\n")
        write(proj / "app.cfg", "port=1\n")
        write(proj / "a.py", "a = 1\n")
        git(proj, "add", "-A")
        git(proj, "add", "-f", "app.cfg")
        git(proj, "commit", "-q", "-m", "init")
        write(proj / "local.cfg", "untracked and ignored\n")
        project, _ = self.init(proj)
        base = self.start_task(project, "t1")
        files = self.tree(project, base)
        self.assertIn("app.cfg", files)
        self.assertNotIn("local.cfg", files)
        write(proj / "app.cfg", "port=9999\n")
        self.assertIn("app.cfg", quiet(baton.cmd_diff, project, Namespace(task="t1", since="base", stat=True)))
        quiet(baton.cmd_rollback, project, Namespace(task="t1", ref=base, yes=True))
        self.assertEqual((proj / "app.cfg").read_text(), "port=1\n")


class StateSafetyTests(Base):
    def legacy_project(self, name="v1p"):
        proj = self.tmp / name
        write(proj / ".baton" / "config.json", "{}")
        write(proj / "a.py", "x\n")
        return proj

    def test_in_flight_v1_leg_is_not_imported(self):
        proj = self.legacy_project()
        runner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "_run", "old", "1"])
        self.addCleanup(runner.kill)
        legacy = {"task": "old", "status": "running",
                  "legs": [{"n": 1, "status": "running", "started_at": time.time(), "runner_pid": runner.pid}]}
        write(proj / ".baton" / "tasks" / "old" / "state.json", json.dumps(legacy))
        project = baton.Project(str(proj))
        self.assertEqual(project.read_state("old")["status"], "running")
        self.assertFalse(Path(project.state_path("old")).exists())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            project.update_state("old", lambda s: s.update(status="x"))
        runner.kill()
        runner.wait()
        self.assertEqual(project.read_state("old")["status"], "running")  # imported now, v1 runner gone
        self.assertTrue(Path(project.state_path("old")).exists())

    def test_v1_launch_window_without_runner_pid_is_not_imported(self):
        proj = self.legacy_project()
        legacy_path = proj / ".baton" / "tasks" / "old" / "state.json"
        write(legacy_path, json.dumps({"task": "old", "status": "running",
                                       "legs": [{"n": 1, "status": "running", "started_at": time.time()}]}))
        project = baton.Project(str(proj))
        self.assertEqual(project.read_state("old")["status"], "running")
        self.assertFalse(Path(project.state_path("old")).exists())
        old = json.loads(legacy_path.read_text())
        old["legs"][0]["started_at"] = time.time() - 31
        legacy_path.write_text(json.dumps(old))
        self.assertEqual(project.read_state("old")["status"], "running")
        self.assertTrue(Path(project.state_path("old")).exists())

    def test_checkpoint_typo_does_not_create_state(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            baton.cmd_checkpoint(project, Namespace(task="typo", label=None))
        self.assertIn("没有简报或状态文件", err.getvalue())
        self.assertFalse(Path(project.state_path("typo")).exists())

    def test_case_only_backup_collision_uses_suffix_and_manifest(self):
        backup = self.tmp / "backup"
        backup.mkdir()
        (backup / "Foo").write_text("old\n")
        manifest = []
        with patch.object(baton.os.path, "lexists",
                          side_effect=lambda path: path == str(backup / "foo")):
            dest = baton._backup_destination(str(backup), "foo", manifest)
        self.assertEqual(dest, str(backup / "foo.2"))
        self.assertEqual(manifest, [{"original": "foo", "backup": "foo.2"}])

    def test_status_supervised_and_checkpoint_refuse_in_flight_v1_before_writes(self):
        proj = self.legacy_project()
        write(proj / ".baton" / "tasks" / "old" / "state.json", json.dumps(
            {"task": "old", "status": "running",
             "legs": [{"n": 1, "kind": "start", "status": "running", "started_at": time.time(),
                        "events": str(proj / ".baton/runs/old/leg-001.events.jsonl"),
                        "log_size_at_start": 0}]}))
        project = baton.Project(str(proj))
        before = (proj / ".baton" / "supervision.md").read_bytes() if (proj / ".baton" / "supervision.md").exists() else None
        out = quiet(baton.cmd_status, project, Namespace(task="old"))
        self.assertIn("未写入 pending_supervision", out)
        self.assertFalse(Path(project.state_path("old")).exists())
        with self.assertRaises(SystemExit):
            quiet(baton.cmd_supervised, project, Namespace(task="old", verdict="on_track", note="ok"))
        with self.assertRaises(SystemExit):
            quiet(baton.cmd_checkpoint, project, Namespace(task="old", label="tick"))
        after = (proj / ".baton" / "supervision.md").read_bytes() if (proj / ".baton" / "supervision.md").exists() else None
        self.assertEqual(before, after)

    def test_moved_project_is_detected_and_relinked(self):
        proj = self.tmp / "projA"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        self.start_task(project, "t1")
        project.update_state("t1", lambda s: s.update(legs=[{"n": 1, "status": "done", "started_at": 1.0,
                                                            "prompt": str(proj / ".baton/runs/t1/leg-001.prompt.md")}]))
        moved = self.tmp / "projB"
        os.rename(proj, moved)
        moved_project = baton.Project(str(moved))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            moved_project.check_identity()
        self.assertIn("--relink", err.getvalue())
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            quiet(baton.cmd_init, moved_project, Namespace(no_statusline=True, accept_boundary=True))
        out = quiet(baton.cmd_init, moved_project, Namespace(no_statusline=True, accept_boundary=True, relink=True))
        self.assertIn("已把", out)
        st = moved_project.read_state("t1")
        self.assertEqual(st["legs"][0]["prompt"], str(moved / ".baton/runs/t1/leg-001.prompt.md"))
        self.assertIn("refs/baton/t1/base", quiet(baton.cmd_refs, moved_project, Namespace(task="t1")))
        moved_project.check_identity()  # no longer complains

    def test_copied_project_refuses_relink_and_fresh_keeps_original_state(self):
        original = self.tmp / "original"
        write(original / "a.py", "x\n")
        project, _ = self.init(original)
        self.start_task(project, "t1")
        copied = self.tmp / "copied"
        shutil.copytree(original, copied)
        copied_project = baton.Project(str(copied))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            quiet(baton.cmd_init, copied_project, Namespace(no_statusline=True, accept_boundary=True, relink=True))
        self.assertIn("复制项目", err.getvalue())
        old_state = Path(project.state_path("t1"))
        self.assertTrue(old_state.exists())
        out = quiet(baton.cmd_init, copied_project, Namespace(no_statusline=True, accept_boundary=True, fresh=True))
        self.assertIn("新的项目外状态目录", out)
        self.assertTrue(old_state.exists())
        self.assertTrue(Path(copied_project.state_dir).is_dir())
        self.assertNotEqual(copied_project.project_id, project.project_id)

    def test_missing_old_state_suggests_fresh(self):
        original = self.tmp / "original"
        write(original / "a.py", "x\n")
        project, _ = self.init(original)
        copied = self.tmp / "copied"
        shutil.copytree(original, copied)
        old_state = Path(project.state_dir)
        shutil.rmtree(old_state)
        copied_project = baton.Project(str(copied))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            quiet(baton.cmd_init, copied_project, Namespace(no_statusline=True, accept_boundary=True, relink=True))
        self.assertIn("--fresh", err.getvalue())

    def test_config_and_doctor_do_not_exit_on_identity_warning(self):
        original = self.tmp / "original"
        write(original / "a.py", "x\n")
        project, _ = self.init(original)
        copied = self.tmp / "copied"
        shutil.copytree(original, copied)
        bindir = self.tmp / "bin"
        write(bindir / "codex", "#!/bin/sh\nexit 0\n")
        executable_fixture(bindir / "codex")
        env = clean_env(HOME=str(self.tmp / "home"), BATON_HOME=str(self.tmp / "state"),
                        PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        for command in ("config", "doctor"):
            with self.subTest(command=command):
                p = subprocess.run([sys.executable, str(BATON_PY), "--root", str(copied), command],
                                   env=env, text=True, capture_output=True)
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("警告：项目目录换过位置或名字", p.stderr)

    def test_launch_refuses_to_overwrite_existing_leg_files(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        write(proj / ".baton" / "runs" / "t1" / "leg-001.prompt.md", "old prompt\n")
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err), \
                patch.object(baton, "safe_quota", return_value=None), \
                patch.object(baton.codex_quota, "describe", return_value="q"):
            quiet(baton.launch_leg, project, "t1", "start", "new prompt")
        self.assertEqual((proj / ".baton" / "runs" / "t1" / "leg-001.prompt.md").read_text(), "old prompt\n")
        self.assertIn("baton init --relink", err.getvalue())

    def test_missing_project_id_is_recorded_by_first_v2_command(self):
        proj = self.tmp / "legacy"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        os.remove(project.id_file)
        env = clean_env(HOME=str(self.tmp / "home"), BATON_HOME=str(self.tmp / "state"))
        p = subprocess.run([sys.executable, str(BATON_PY), "--root", str(proj), "config"],
                           env=env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(project.recorded_id(), project.project_id)

    def test_init_of_a_missing_subdir_still_asks_for_the_boundary(self):
        outer = self.tmp / "outer"
        outer.mkdir()
        git(outer, "init", "-q")
        project = baton.Project(str(outer / "newproj"))
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            quiet(baton.cmd_init, project, Namespace(no_statusline=True, accept_boundary=False))
        self.assertFalse((outer / "newproj").exists())

    def test_letter_case_of_root_does_not_change_the_project(self):
        proj = self.tmp / "MyProj"
        write(proj / "a.py", "x\n")
        if not os.path.exists(str(self.tmp / "myproj")):
            self.skipTest("case-sensitive file system")
        self.assertEqual(baton.Project(str(self.tmp / "myproj")).project_id, baton.Project(str(proj)).project_id)

    def test_stale_index_lock_does_not_block_snapshots(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        write(Path(project.snap_git_dir) / "index.lock", "")
        self.assertTrue(project.store().snapshot("probe"))

    def test_state_dir_inside_the_project_is_refused(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        with patch.dict(os.environ, {"BATON_HOME": str(proj / "state")}):
            with self.assertRaises(RuntimeError):
                baton.ensure_shadow(baton.Project(str(proj)))

    def test_diff_against_an_unknown_ref_fails_loudly(self):
        proj = self.tmp / "p"
        write(proj / "a.py", "x\n")
        project, _ = self.init(proj)
        self.start_task(project, "t1")
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            quiet(baton.cmd_diff, project, Namespace(task="t1", since="HEAD", stat=True))
        self.assertIn("找不到回滚点", err.getvalue())

    def test_init_ignores_an_early_v1_dot_baton_above(self):
        vault = self.tmp / "vault"
        vault.mkdir()
        git(vault, "init", "-q")
        write(vault / ".baton" / "config.json", "{}")
        sub = vault / "Bica"
        sub.mkdir()
        self.assertEqual(baton.find_project_dir(str(sub)), str(vault))
        self.assertEqual(baton.find_project_dir(str(sub), legacy=False), str(sub))


class CliEndToEndTests(Base):
    """The real CLI with a fake codex, in a ~/yuzz-like layout; the user's repo must not change."""

    def test_full_cycle_leaves_the_user_repo_untouched(self):
        self.assertFalse((ROOT / "a.py").exists(), "仓库根目录不应有端到端试验的 a.py")
        self.addCleanup(lambda: self.assertFalse(
            (ROOT / "a.py").exists(), "端到端试验写入了 Baton 仓库根目录"))
        bindir = self.tmp / "bin"
        write(bindir / "codex", textwrap.dedent('''\
            #!/usr/bin/env python3
            import json, sys
            args = sys.argv[1:]
            if not args or args[0] != "exec":
                sys.exit(2)
            out = args[args.index("-o") + 1]
            sys.stdin.read()
            if "--ignore-user-config" in args:
                json.dump({"summary": "monitor ok", "recommendation": "APPROVE", "findings": [], "memory_advice": []}, open(out, "w"))
                sys.exit(0)
            print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
            with open("a.py", "a") as fh:
                fh.write("edited = True\\n")
            with open(".baton/log.md", "a") as fh:
                fh.write("\\n## fake · minor · edit\\n")
            json.dump({"status": "report", "summary": "fake", "artifact": "", "change_size": "minor"}, open(out, "w"))
            '''))
        executable_fixture(bindir / "codex")
        outer = self.tmp / "outer"
        outer.mkdir()
        git(outer, "init", "-q")
        write(outer / ".gitignore", "*\n!*/\n!*.md\n!.gitignore\n")
        write(outer / "README.md", "hi\n")
        git(outer, "add", "-A")
        git(outer, "commit", "-q", "-m", "init")
        write(outer / "README.md", "staged change\n")
        git(outer, "add", "README.md")
        proj = outer / "proj"
        write(proj / "a.py", "a = 1\n")
        write(proj / "baton.json", json.dumps({"executor": {"backend": "codex"}}))
        env = clean_env(HOME=str(self.tmp / "home"), BATON_HOME=str(self.tmp / "state"),
                        PATH=str(bindir) + os.pathsep + os.environ["PATH"], BATON_DESKTOP_NOTIFY="0")

        def fingerprint():
            idx = (outer / ".git" / "index").read_bytes()
            return (hashlib.sha1(idx).hexdigest(), git(outer, "rev-parse", "HEAD"), git(outer, "for-each-ref"),
                    git(outer, "status", "--porcelain", "--untracked-files=no"))

        before = fingerprint()

        def b(*args, rc=0):
            p = subprocess.run([sys.executable, str(BATON_PY), *args], cwd=str(proj), env=env,
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(p.returncode, rc, f"baton {args}: {p.stdout}\n{p.stderr}")
            return p.stdout

        b("init", "--no-statusline", rc=1)
        b("init", "--no-statusline", "--accept-boundary")
        b("new", "t")
        write(proj / ".baton" / "tasks" / "t" / "brief.md",
              '```baton-scope\n{"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}\n```\n# 任务简报：t\n\n改 a.py\n')
        b("start", "t")
        self.assertIn("BATON::EVENT REPORT", b("wait", "t"))
        self.assertIn("a.py", b("status", "t"))
        b("supervised", "t", "--verdict", "on_track", "--note", "ok")
        self.assertIn("a.py", b("diff", "t", "--since", "base", "--stat"))
        self.assertIn("refs/baton/t/leg-001-end", b("refs", "t"))
        b("rollback", "t", "refs/baton/t/base", "--yes")
        self.assertEqual((proj / "a.py").read_text(), "a = 1\n")
        self.assertEqual(fingerprint(), before)
        self.assertFalse((ROOT / "a.py").exists(), "端到端试验只能写临时项目")


if __name__ == "__main__":
    unittest.main()
