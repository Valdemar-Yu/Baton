from platform_fixtures import executable_fixture
import json
import os
import tempfile
import unittest
import stat
import io
import contextlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))

from baton_memory.ledger import LedgerCorrupt, MemoryLedger, memory_append_approved
from baton_memory.search import fuse_results, keyword_search, render_memory_context, render_pinned_block
from baton_memory.server import _memory_search_impl, archive_search_impl


class FakeIndex:
    def __init__(self, fail=False):
        self.fail = fail
        self.added = []

    def add(self, record):
        if self.fail:
            raise RuntimeError("Ollama 不在线")
        self.added.append(record["id"])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".baton" / "runs" / "task").mkdir(parents=True)
        (self.root / "src.py").write_text("def keep():\n    return 1\n", encoding="utf-8")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"BATON_HOME": str(self.home), "BATON_TASK": "task", "BATON_LEG": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.ledger = MemoryLedger(str(self.root))

    def entry(self, **kwargs):
        value = {"kind": "command", "text": "pytest -q tests", "source": "R001",
                 "anchors": ["src.py#keep"], "task": "task", "leg": 1, "end_snapshot": "abc"}
        value.update(kwargs)
        return value

    def test_hash_chain_export_and_status_append(self):
        records = self.ledger.append([self.entry(), self.entry(kind="event", text="did work")])
        self.assertEqual([x["id"] for x in records], ["M001", "M002"])
        self.assertTrue(self.ledger.verify()[0])
        self.assertIn("M001", Path(self.ledger.export()).read_text())
        self.ledger.append_status("M001", "superseded", replaced_by="M002")
        self.assertEqual(self.ledger.latest()["M001"]["status"], "superseded")
        self.assertTrue(self.ledger.verify()[0])

    def test_stale_head_and_partial_tail_are_repaired(self):
        self.ledger.append([self.entry(), self.entry(text="second")])
        records = self.ledger.records()
        Path(self.ledger.head_path).write_text(records[0]["record_hash"] + "\n")
        self.assertTrue(self.ledger.verify()[0])
        with open(self.ledger.ledger_path, "ab") as stream:
            stream.write(b'{"partial":')
        self.assertTrue(self.ledger.verify()[0])
        self.assertEqual(len(self.ledger.records()), 2)

    def test_bad_record_is_reported(self):
        self.ledger.append([self.entry()])
        raw = Path(self.ledger.ledger_path).read_text()
        Path(self.ledger.ledger_path).write_text(raw.replace("pytest -q tests", "changed"), encoding="utf-8")
        good, error = self.ledger.verify(repair=False)
        self.assertFalse(good)
        self.assertIn("第 1 条", error)

    def test_missing_head_is_corruption(self):
        self.ledger.append([self.entry()])
        Path(self.ledger.head_path).unlink()
        good, error = self.ledger.verify(repair=True)
        self.assertFalse(good)
        self.assertIn("head 缺失", error)

    def test_chinese_ngrams_are_strings_and_no_single_character_hits(self):
        self.ledger.append([self.entry(text="集成测试必须在 tests/integration 运行")])
        self.assertEqual(keyword_search(self.ledger, "集成测试")[0]["ledger_id"], "M001")
        self.assertEqual(keyword_search(self.ledger, "的"), [])

    def test_semantic_non_availability_errors_are_not_swallowed(self):
        self.ledger.append([self.entry(text="数据加载代码放在 src/data/")])
        class BadSemantic:
            def search(self, *args, **kwargs):
                raise TypeError("signature mismatch")
        from baton_memory.search import retrieve_memory
        with self.assertRaises(TypeError):
            retrieve_memory(self.ledger, ["数据读取应该写在哪里"], semantic=BadSemantic())

    def test_mem0_config_uses_tag_and_supported_embedder_keys(self):
        from baton_memory.mem0_adapter import MODEL_NAME, mem0_config
        config = mem0_config(self.ledger)
        self.assertEqual(MODEL_NAME, "baton-qwen3-embed:latest")
        self.assertNotIn("keep_alive", config["embedder"]["config"])

    def test_hermes_profile_upgrade_is_explicit_and_prints_diff(self):
        import baton
        path = Path(self.tmp.name) / "config.yaml"
        path.write_text("old\n", encoding="utf-8")
        cfg = {"executor": {"hermes_profile": "baton-exec-v2"}}
        baton._write_readonly_profile_file(str(path), "new\n", cfg)
        self.assertEqual(path.read_text(), "old\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton._write_readonly_profile_file(str(path), "new\n", cfg, upgrade=True)
        self.assertEqual(path.read_text(), "new\n")
        self.assertIn("---", out.getvalue())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444)

    def test_memory_prompt_uses_venv_subprocess(self):
        import baton
        (self.root / ".baton" / "tasks" / "task").mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "tasks" / "task" / "brief.md").write_text("目标\n", encoding="utf-8")
        self.ledger.append([self.entry()])
        fake_python = Path(baton.platform_support.venv_python(str(self.home)))
        fake_python.parent.mkdir(parents=True, exist_ok=True)
        fake_python.write_text("#!" + sys.executable + "\nimport sys,json; sys.stdin.read(); print(json.dumps({'ok':True,'context':'FAKE_VENV_CONTEXT','core':'','pushed':['M001'],'withdrawals':[]}))", encoding="utf-8")
        fake_python = executable_fixture(fake_python)
        project = baton.Project(str(self.root))
        with patch.dict(os.environ, {"BATON_HOME": str(self.home)}):
            text, meta = baton.memory_prompt_for_leg(project, "task", "指令", st={}, cfg=baton.deep_merge(project.config, {"memory": {"backend": "mem0"}}))
        self.assertIn("FAKE_VENV_CONTEXT", text)
        self.assertEqual(meta["pushed"], ["M001"])

    def test_keyword_fallback_accepts_shortstat_diff_from_prior_leg(self):
        # A finished leg stores diff as the [files, insertions, deletions] shortstat, not path records.
        import baton
        (self.root / ".baton" / "tasks" / "task").mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "tasks" / "task" / "brief.md").write_text("目标\n", encoding="utf-8")
        self.ledger.append([self.entry()])
        project = baton.Project(str(self.root))
        st = {"legs": [{"n": 1, "diff": [4, 195, 96]}]}
        cfg = baton.deep_merge(project.config, {"memory": {"backend": "local"}})
        text, meta = baton.memory_prompt_for_leg(project, "task", "指令", st=st, cfg=cfg)
        self.assertIn("指令", text)

    def test_profile_config_passes_baton_home_to_mcp(self):
        import baton
        fake = Path(baton.platform_support.venv_python(str(self.home)))
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.write_text("", encoding="utf-8")
        cfg = {"executor": {"provider": "openai-codex"},
               "project": {"state_dir": str(self.home / "projects" / "p")}}
        self.assertIn('BATON_HOME: "${BATON_HOME}"', baton.hermes_config_text(cfg))

    def test_index_pending_keeps_ledger_ack(self):
        fake = FakeIndex(fail=True)
        records = memory_append_approved(self.ledger, [self.entry()], source="R001", mem0_writer=fake)
        self.assertEqual(records[0]["id"], "M001")
        self.assertEqual(self.ledger.latest()["M001"]["status"], "active")
        self.assertTrue(self.ledger.latest()["M001"]["index_pending"])

    def test_keyword_fusion_limits_and_core_dedup(self):
        self.ledger.append([
            self.entry(text="event path", kind="event"), self.entry(text="map path", kind="map"),
            self.entry(text="event path 2", kind="event"), self.entry(text="map path 2", kind="map"),
            self.entry(text="rule path", kind="command", pinned=True),
        ])
        values = keyword_search(self.ledger, "path", 30)
        fused = fuse_results(self.ledger, values, [], top_k=10, core_ids=["M005"])
        self.assertLessEqual(sum(x["kind"] == "event" for x in fused), 2)
        self.assertLessEqual(sum(x["kind"] == "map" for x in fused), 2)
        self.assertNotIn("M005", [x["ledger_id"] for x in fused])
        block, truncated, ids = render_pinned_block(self.ledger)
        self.assertIn("M005", ids)
        self.assertFalse(truncated)
        self.assertIn("相关记忆", render_memory_context(self.ledger, fused))

    def test_anchor_statuses(self):
        self.ledger.append([self.entry()])
        item = self.ledger.latest()["M001"]
        self.assertIn("已核实", render_memory_context(self.ledger, [dict(item, ledger_id="M001", verified=True)]))
        item["anchors"] = ["gone.py#x"]
        self.assertIn("文件已不存在", render_memory_context(self.ledger, [dict(item, ledger_id="M001")]))
        item["anchors"] = ["src.py#gone"]
        self.assertIn("相关代码已改动", render_memory_context(self.ledger, [dict(item, ledger_id="M001")]))
        item["anchors"] = ["src.py:keep"]
        self.assertNotIn("文件已不存在", render_memory_context(self.ledger, [dict(item, ledger_id="M001")]))
        self.assertNotIn("相关代码已改动", render_memory_context(self.ledger, [dict(item, ledger_id="M001")]))

    def test_withdrawal_is_rendered_once(self):
        self.ledger.append([self.entry()])
        text = render_memory_context(self.ledger, [], ["撤回：M001 已由 M002 取代，忽略此前轮次推送的 M001"])
        self.assertEqual(text.count("撤回：M001"), 1)

    def test_server_tools_archive_and_unexpanded_record(self):
        self.ledger.append([self.entry()])
        (self.root / ".baton" / "log.md").write_text("M001 pytest -q tests\n", encoding="utf-8")
        with patch.dict(os.environ, {"BATON_TASK": "${BATON_TASK}", "BATON_LEG": "${BATON_LEG}",
                                     "BATON_LEG_STARTED": "${BATON_LEG_STARTED}", "BATON_HOME": str(self.home)}):
            with patch("baton_memory.server._find_project", return_value=str(self.root)):
                result = _memory_search_impl(["pytest"])
        self.assertEqual(result["results"][0]["ledger_id"], "M001")
        self.assertIn("未展开", result["note"])
        self.assertFalse(list((self.root / ".baton" / "runs" / "task").glob("*.memory-calls.jsonl")))
        archive = archive_search_impl("pytest", str(self.root))
        self.assertEqual(archive[0]["file"], "log.md")

    def test_server_unexpanded_baton_home_uses_default_and_notes(self):
        old_cwd = os.getcwd()
        os.chdir(self.root)
        try:
            with patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "BATON_HOME": "${BATON_HOME}", "BATON_TASK": "task",
                                         "BATON_LEG": "1", "BATON_LEG_STARTED": "1"}):
                with patch("baton_memory.server._find_project", return_value=str(self.root)):
                    result = _memory_search_impl(["pytest"])
        finally:
            os.chdir(old_cwd)
        self.assertIn("BATON_HOME 未展开", result.get("note", ""))
        self.assertTrue((self.home / ".baton" / "projects").is_dir())
        self.assertFalse((ROOT / "${BATON_HOME}").exists())


if __name__ == "__main__":
    unittest.main()

