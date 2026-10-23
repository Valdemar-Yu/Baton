import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x")
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    return subprocess.run(["git", "-C", str(cwd), *args], env=env, check=True,
                          capture_output=True, text=True).stdout


def quiet(fn, *args, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args, **kw)
    return out.getvalue()


class ShadowRepoTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.tmp / "home"), "USERPROFILE": str(self.tmp / "home"), "BATON_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)

    def whitelist_layout(self):
        """An enclosing repo whose .gitignore ignores everything but *.md, like ~/yuzz."""
        outer = self.tmp / "outer"
        outer.mkdir()
        git(outer, "init", "-q")
        (outer / ".gitignore").write_text("*\n!*/\n!*.md\n!.gitignore\n", encoding="utf-8")
        proj = outer / "proj"
        (proj / "src" / "models").mkdir(parents=True)
        (proj / "models").mkdir()
        (proj / "baton.json").write_text(json.dumps({"judge": {"model": "opus"}}), encoding="utf-8")
        (proj / "a.py").write_text("print(1)\n", encoding="utf-8")
        (proj / "src" / "models" / "m.py").write_text("x = 1\n", encoding="utf-8")
        (proj / "models" / "big.bin").write_text("BIG\n", encoding="utf-8")
        (proj / "README.md").write_text("hi\n", encoding="utf-8")
        return outer, proj

    def init(self, proj):
        path = Path(proj) / "baton.json"
        if not path.exists():
            path.write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        project = baton.Project(str(proj))
        out = quiet(baton.cmd_init, project, Namespace(no_statusline=True, accept_boundary=True))
        return project, out

    def start_task(self, project, task):
        project.update_state(task, lambda s: s.update(task=task, snapshot_store="shadow"))
        ref, err = baton.try_checkpoint(project, task, "base")
        self.assertIsNone(err)
        project.update_state(task, lambda s: s.update(base_ref=ref))
        return ref

    def test_snapshot_covers_files_the_enclosing_repo_ignores(self):
        outer, proj = self.whitelist_layout()
        project, _ = self.init(proj)
        store = project.store()
        self.assertEqual(store.kind, "shadow")
        commit = store.snapshot("probe")
        files = store.git("ls-tree", "-r", "--name-only", commit).split()
        self.assertIn("a.py", files)
        self.assertIn("src/models/m.py", files)  # only the top-level models/ is excluded
        self.assertNotIn("models/big.bin", files)
        self.assertEqual(git(outer, "for-each-ref").strip(), "")  # user refs untouched
        self.assertFalse((outer / ".git" / "index").exists())  # user index untouched
        self.assertTrue(Path(project.snap_git_dir, "HEAD").is_file())

    def test_diff_refs_and_rollback_use_the_shadow_repo(self):
        outer, proj = self.whitelist_layout()
        project, _ = self.init(proj)
        base = self.start_task(project, "t1")
        self.assertEqual(base, "refs/baton/t1/base")
        (proj / "a.py").write_text("print(2)\n", encoding="utf-8")
        (proj / "new.py").write_text("y = 2\n", encoding="utf-8")
        diff = quiet(baton.cmd_diff, project, Namespace(task="t1", since="base", stat=False))
        self.assertIn("a.py", diff)
        self.assertIn("print(2)", diff)
        self.assertIn("new.py", diff)
        refs = quiet(baton.cmd_refs, project, Namespace(task="t1"))
        self.assertIn("refs/baton/t1/base", refs)
        out = quiet(baton.cmd_rollback, project, Namespace(task="t1", ref=base, yes=True))
        self.assertIn("已把工作区恢复到", out)
        self.assertEqual((proj / "a.py").read_text(encoding="utf-8"), "print(1)\n")
        self.assertFalse((proj / "new.py").exists())
        backups = Path(project.state_dir) / "rollback-backup"
        self.assertTrue(any(p.name == "new.py" for p in backups.rglob("new.py")))
        self.assertFalse((proj / ".baton" / "rollback-backup").exists())
        self.assertEqual(git(outer, "for-each-ref").strip(), "")
        self.assertIn("pre-rollback-", quiet(baton.cmd_refs, project, Namespace(task="t1")))

    def test_non_git_project_has_checkpoints_and_rollback(self):
        proj = self.tmp / "plain"
        proj.mkdir()
        (proj / "a.py").write_text("v1\n", encoding="utf-8")
        project, out = self.init(proj)
        self.assertNotIn("不是 git 仓库，回滚点", out)
        base = self.start_task(project, "t2")
        (proj / "a.py").write_text("v2\n", encoding="utf-8")
        quiet(baton.cmd_rollback, project, Namespace(task="t2", ref=base, yes=True))
        self.assertEqual((proj / "a.py").read_text(encoding="utf-8"), "v1\n")

    def test_v1_task_keeps_checkpoints_in_the_user_repo(self):
        proj = self.tmp / "v1proj"
        proj.mkdir()
        git(proj, "init", "-q")
        (proj / "a.py").write_text("old\n", encoding="utf-8")
        git(proj, "add", "a.py")
        git(proj, "commit", "-q", "-m", "init")
        (proj / ".baton" / "tasks" / "old").mkdir(parents=True)
        (proj / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        commit = baton.snapshot(str(proj), "v1 checkpoint")
        git(proj, "update-ref", "refs/baton/old/base", commit)
        legacy = {"task": "old", "status": "idle", "base_ref": "refs/baton/old/base",
                  "legs": [{"n": 1, "status": "done", "started_at": 1.0}]}
        (proj / ".baton" / "tasks" / "old" / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
        project = baton.Project(str(proj))
        store = project.store("old")
        self.assertEqual(store.kind, "repo")
        self.assertIn("refs/baton/old/base", quiet(baton.cmd_refs, project, Namespace(task="old")))
        (proj / "a.py").write_text("changed\n", encoding="utf-8")
        self.assertIn("changed", quiet(baton.cmd_diff, project, Namespace(task="old", since="base", stat=False)))
        quiet(baton.cmd_rollback, project, Namespace(task="old", ref="refs/baton/old/base", yes=True))
        self.assertEqual((proj / "a.py").read_text(encoding="utf-8"), "old\n")
        self.assertIn("refs/baton/old/pre-rollback-", git(proj, "for-each-ref", "refs/baton/old"))
        self.assertFalse(Path(project.snap_git_dir).exists())

    def test_v1_task_without_refs_moves_to_the_shadow_repo(self):
        # a v1 task in a non-git project whose first leg failed early: legs, no refs, no thread
        proj = self.tmp / "v1plain"
        (proj / ".baton" / "tasks" / "old").mkdir(parents=True)
        (proj / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        (proj / ".baton" / "tasks" / "old" / "brief.md").write_text("# brief\n\nfilled\n", encoding="utf-8")
        (proj / "a.py").write_text("v1\n", encoding="utf-8")
        (proj / ".baton" / "tasks" / "old" / "state.json").write_text(json.dumps(
            {"task": "old", "legs": [{"n": 1, "status": "failed", "started_at": 1.0,
                                     "start_ref": None, "end_ref": None}]}), encoding="utf-8")
        project = baton.Project(str(proj))
        self.assertEqual(project.store("old").kind, "shadow")
        with patch.object(baton, "launch_leg") as launch, patch.object(baton, "protocol_text", return_value="P"):
            baton.cmd_start(project, Namespace(task="old"))  # v1 could restart it; v2 must too
        launch.assert_called_once()
        st = project.read_state("old")
        self.assertEqual(st["snapshot_store"], "shadow")
        self.assertEqual(st["base_ref"], "refs/baton/old/base")
        self.assertEqual(project.store("old").kind, "shadow")  # still shadow once refs exist

    def test_init_reports_scope_and_keeps_edited_excludes(self):
        outer, proj = self.whitelist_layout()
        project, out = self.init(proj)
        self.assertIn("快照覆盖范围", out)
        lines = {l.split()[-1]: l.split()[0] for l in out.splitlines() if l.startswith("  ")}
        self.assertEqual(lines.get("models/"), "排除")
        self.assertEqual(lines.get("src/"), "快照")
        self.assertEqual(lines.get("a.py"), "快照")
        exclude = Path(project.snap_git_dir) / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "/scratch/\n", encoding="utf-8")
        self.init(proj)
        text = exclude.read_text(encoding="utf-8")
        self.assertIn("/scratch/", text)
        self.assertEqual(text.count(baton.SHADOW_EXCLUDE_MARK), 1)

    def test_start_marks_new_tasks_as_shadow(self):
        outer, proj = self.whitelist_layout()
        project, _ = self.init(proj)
        task_dir = proj / ".baton" / "tasks" / "t3"
        task_dir.mkdir(parents=True)
        (task_dir / "brief.md").write_text(
            '```baton-scope\n{"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}\n```\n# brief\n\nfilled\n', encoding="utf-8")
        with patch.object(baton, "launch_leg") as launch, patch.object(baton, "protocol_text", return_value="P"):
            baton.cmd_start(project, Namespace(task="t3"))
        launch.assert_called_once()
        st = project.read_state("t3")
        self.assertEqual(st["snapshot_store"], "shadow")
        self.assertEqual(st["base_ref"], "refs/baton/t3/base")
        self.assertIn("refs/baton/t3/base", project.store("t3").git("for-each-ref"))
        self.assertEqual(git(outer, "for-each-ref").strip(), "")


class ProjectMarkerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        env = patch.dict(os.environ, {"HOME": str(self.tmp / "home"), "USERPROFILE": str(self.tmp / "home"), "BATON_HOME": str(self.tmp / "state")})
        env.start()
        self.addCleanup(env.stop)

    def test_initialized_dot_baton_marks_an_early_v1_project(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".baton").mkdir()
        (repo / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        start = repo / "src" / "pkg"
        start.mkdir(parents=True)
        self.assertEqual(baton.find_project_dir(str(start)), os.path.realpath(str(repo)))

    def test_state_root_does_not_mark_a_project(self):
        repo = self.tmp / "home2"
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".baton" / "projects").mkdir(parents=True)  # what the state root looks like
        start = repo / "work"
        start.mkdir()
        self.assertEqual(baton.find_project_dir(str(start)), os.path.realpath(str(start)))

    def test_config_works_without_init(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        env = dict(os.environ)
        p = subprocess.run([sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(plain),
                            "config"], env=env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("项目外状态目录", p.stdout)


class GitOutputDecodingTests(unittest.TestCase):
    def test_probe_blob_reads_utf16_file(self):
        # PowerShell 5.1 `>` writes UTF-16LE with a BOM; git show must not kill the runner.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q")
            (repo / "inventory.json").write_bytes('{"gpu": 0}'.encode("utf-16"))
            git(repo, "add", "inventory.json")
            git(repo, "commit", "-q", "-m", "x")
            store = Namespace(git=lambda *args, check=True: baton.run(["git", "-C", str(repo), *args], check=check))
            text = baton._probe_blob(store, "HEAD", "inventory.json")
        self.assertIsInstance(text, str)
        self.assertTrue(text.startswith("�"))


if __name__ == "__main__":
    unittest.main()
