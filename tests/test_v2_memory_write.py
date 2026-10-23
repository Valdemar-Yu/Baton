import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))

import baton
from baton_memory.candidates import number_candidates, parse_memory_candidates
from baton_memory.ledger import MemoryLedger


class FakeProject:
    def __init__(self, root, home, state):
        self.root = str(root)
        self.project_id = "write-test"
        self.state_dir = str(Path(home) / "projects" / self.project_id)
        self.config = {"memory": {"backend": "local"}, "monitor": {"enabled": True},
                       "mode": "standard", "change_size": {"major_files": 4, "major_lines": 200},
                       "supervision": {"test_command": ""}}
        self._state = state

    def load_state(self, task):
        return self._state

    def read_state(self, task, default=None):
        return self._state

    def p(self, *parts):
        return os.path.join(self.root, ".baton", *parts)

    def task_dir(self, task):
        return self.p("tasks", task)

    def state_path(self, task):
        return os.path.join(self.state_dir, "tasks", task, "state.json")


class MemoryWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "baton-home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"BATON_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_report_candidates_are_parsed_and_numbered(self):
        report = """<section><h2>记忆候选</h2>
<pre data-memory-candidates="true">[{"kind":"command","text":"pytest -q tests","source":"executor","anchors":["tests/"]}, {"type":"pitfall","text":"错误信息原样保留","source":"executor","anchors":["src/a.py#run"]}]</pre>
</section><section><h2>申辩</h2></section>"""
        values = number_candidates(parse_memory_candidates(report))
        self.assertEqual([x["id"] for x in values], ["C1", "C2"])
        self.assertEqual(values[1]["kind"], "pitfall")
        self.assertEqual(parse_memory_candidates("<h2>记忆候选</h2><pre data-memory-candidates=\"true\">[]</pre>"), [])

    def test_html_escaped_pre_block_candidates_are_parsed(self):
        report = ('<h2>记忆候选</h2><pre data-memory-candidates="true">[{&quot;kind&quot;:&quot;pitfall&quot;,'
                  '&quot;text&quot;:&quot;a &amp;&amp; b&quot;,&quot;source&quot;:&quot;executor&quot;,'
                  '&quot;anchors&quot;:[&quot;src/a.py&quot;]}]</pre>')
        values = parse_memory_candidates(report)
        self.assertEqual(values[0]["text"], "a && b")

    def test_monitor_memory_advice_is_strict_object_array(self):
        valid = {"summary": "ok", "recommendation": "APPROVE", "findings": [],
                 "memory_advice": [{"id": "C1", "action": "approve", "kind": "command",
                                    "text": "pytest -q tests", "source": "executor",
                                    "anchors": ["tests/"], "reason": "本轮成功执行"}]}
        self.assertEqual(baton._validate_monitor_brief(valid, "report", ["C1"]), [])
        invalid = dict(valid, memory_advice=["C1"])
        self.assertTrue(baton._validate_monitor_brief(invalid, "report", ["C1"]))
        invalid = dict(valid, memory_advice=[dict(valid["memory_advice"][0], id="C9")])
        self.assertTrue(baton._validate_monitor_brief(invalid, "report", ["C1"]))

    def test_verdict_memory_overrides_are_parsed(self):
        text = "```baton-verdict\n" + json.dumps({"conclusion": "APPROVE", "findings": {},
            "memory": {"C1": "reject", "new-1": {"text": "改写后的正文"}}}, ensure_ascii=False) + "\n```\n"
        value, _ = baton._parse_verdict(text, "report", brief_json={"findings": []})
        self.assertEqual(value["memory"]["C1"], "reject")
        self.assertEqual(value["memory"]["new-1"]["text"], "改写后的正文")

    def test_command_verification_requires_exact_simple_successful_call(self):
        events = Path(self.tmp.name) / "events.jsonl"
        events.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "/bin/zsh -lc \"cd /tmp/project && pytest -q tests\"", "exit_code": 0}}) + "\n",
            encoding="utf-8")
        project = FakeProject(self.root, self.home, {})
        leg = {"events": str(events), "started_at": 0}
        self.assertTrue(baton.verify_memory_command(project, "task", leg, "pytest -q tests"))
        events.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "/bin/bash -lc \"pytest -q tests | tee out\"", "exit_code": 0}}) + "\n",
            encoding="utf-8")
        self.assertFalse(baton.verify_memory_command(project, "task", leg, "pytest -q tests"))
        events.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pytest -q tests", "exit_code": 1}}) + "\n",
            encoding="utf-8")
        self.assertFalse(baton.verify_memory_command(project, "task", leg, "pytest -q tests"))

    def test_dossier_candidates_include_three_nearest_and_decision_precedent(self):
        project = FakeProject(self.root, self.home, {})
        ledger = MemoryLedger(project)
        ledger.append([{"kind": "map", "text": "pytest tests/integration", "source": "old",
                         "anchors": ["tests/"], "task": "old", "leg": 1, "end_snapshot": "s"},
                        {"kind": "event", "text": "tests/integration fixture", "source": "old",
                         "anchors": ["tests/"], "task": "old", "leg": 2, "end_snapshot": "s"},
                        {"kind": "pitfall", "text": "pytest output error", "source": "old",
                         "anchors": ["tests/"], "task": "old", "leg": 3, "end_snapshot": "s"},
                        {"kind": "map", "text": "unrelated module", "source": "old",
                         "anchors": ["src/"], "task": "old", "leg": 4, "end_snapshot": "s"}])
        baton.append_ledger(project, {"type": "decision", "task": "task", "decision_id": "D001",
                                      "decision": "所有命令必须保留原样"})
        artifact = Path(self.tmp.name) / "report.html"
        artifact.write_text("<h2>记忆候选</h2><pre data-memory-candidates=\"true\">" + json.dumps([
            {"kind": "command", "text": "pytest tests/integration", "source": "executor", "anchors": ["tests/"]}
        ]) + "</pre>", encoding="utf-8")
        candidates = baton._dossier_memory_candidates(project, "task", {}, {"n": 1}, artifact.read_text())
        self.assertEqual([x["id"] for x in candidates], ["C1", "C2"])
        self.assertLessEqual(len(candidates[0]["nearest"]), 3)
        self.assertEqual(candidates[1]["kind"], "precedent")
    def test_as_advised_writes_verified_command_and_rejects_unverified_convention(self):
        artifact = Path(self.tmp.name) / "report.html"
        artifact.write_text("<h2>记忆候选</h2><pre data-memory-candidates=\"true\">" + json.dumps([
            {"kind": "command", "text": "pytest -q tests", "source": "executor", "anchors": ["tests/"]},
            {"kind": "convention", "text": "所有模块都用 foo", "source": "executor", "anchors": ["src/"]}
        ]) + "</pre>", encoding="utf-8")
        events = Path(self.tmp.name) / "events.jsonl"
        events.write_text(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pytest -q tests", "exit_code": 0}}) + "\n", encoding="utf-8")
        state = {"legs": [{"n": 1, "result": {"artifact": str(artifact)}, "events": str(events),
                            "started_at": 0, "end_ref": "refs/baton/task/end",
                            "dossier": {"id": "task-leg-001"}}]}
        project = FakeProject(self.root, self.home, state)
        with patch.object(baton, "verify_dossier", return_value=(True, "")), patch.object(
                baton, "_memory_candidates_for_rule", return_value=[
                    {"id": "C1", "kind": "command", "text": "pytest -q tests", "source": "executor", "anchors": ["tests/"]},
                    {"id": "C2", "kind": "convention", "text": "所有模块都用 foo", "source": "executor", "anchors": ["src/"]}]):
            entries = baton._approved_memory_entries(project, "task", "task-leg-001", state["legs"][0],
            {"memory_advice": [
                {"id": "C1", "action": "approve", "kind": "command", "text": "pytest -q tests",
                 "source": "executor", "anchors": ["tests/"], "reason": "verified"},
                {"id": "C2", "action": "approve", "kind": "convention", "text": "所有模块都用 foo",
                 "source": "executor", "anchors": ["src/"], "reason": "not executed"}]})
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["verified"])
        self.assertEqual(entries[0]["source"], "task-leg-001")
        ledger = MemoryLedger(project)
        from baton_memory.ledger import memory_append_approved
        written = memory_append_approved(ledger, entries, source="task-leg-001", mem0_writer=None)
        self.assertEqual(written[0]["candidate_id"], "C1")
        self.assertEqual(ledger.latest()["M001"]["end_snapshot"], "refs/baton/task/end")

    def test_curation_schema_verdict_and_operations(self):
        operation = {"id": "K1", "op": "merge", "targets": ["M001", "M002"], "kind": "map",
                     "text": "入口统一在 src/main.py", "source": "monitor", "anchors": ["src/main.py"],
                     "reason": "重复条目"}
        self.assertEqual(baton._validate_curation({"summary": "整理", "operations": [operation]}), [])
        value, _ = baton._parse_curation_verdict(
            "```baton-verdict\n" + json.dumps({"operations": {"K1": "approve"}}) + "\n```\n",
            {"operations": [operation]})
        self.assertEqual(value["operations"]["K1"], "approve")
        project = FakeProject(self.root, self.home, {})
        ledger = MemoryLedger(project)
        ledger.append([
            {"kind": "map", "text": "入口在 src/main.py", "source": "old", "anchors": ["src/main.py"]},
            {"kind": "map", "text": "main.py 是入口", "source": "old", "anchors": ["src/main.py"]},
        ])
        brief = {"operations": [operation]}
        written, accepted = baton._apply_curation_operations(project, "task", "task-curate-01", brief,
                                                              {"K1": "approve"})
        self.assertEqual(accepted, ["K1"])
        self.assertEqual(len(written), 1)
        latest = ledger.latest()
        self.assertEqual(latest["M001"]["status"], "superseded")
        self.assertEqual(latest["M001"]["replaced_by"], written[0]["id"])

    def test_seed_sources_include_project_protocol_and_log(self):
        (self.root / "AGENTS.md").write_text("项目约定：使用 pytest\n", encoding="utf-8")
        (self.root / ".baton").mkdir(exist_ok=True)
        (self.root / ".baton" / "log.md").write_text("## R001\n完成初始化\n", encoding="utf-8")
        project = FakeProject(self.root, self.home, {})
        values = baton._seed_candidates(project)
        self.assertGreaterEqual(len(values), 2)
        self.assertEqual(values[0]["id"], "C1")
        self.assertTrue(any(item["kind"] == "convention" for item in values))

    def test_rollback_suspends_memories_from_later_snapshot(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.root), "config", "user.name", "t"], check=True)
        (self.root / "a.py").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "a.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "target"], check=True)
        target = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        (self.root / "a.py").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "a.py"], check=True)
        subprocess.run(["git", "-C", str(self.root), "commit", "-qm", "later"], check=True)
        later = subprocess.check_output(["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        project = FakeProject(self.root, self.home, {})
        MemoryLedger(project).append([{"kind": "event", "text": "later fact", "source": "executor",
                                       "anchors": ["a.py"], "end_snapshot": later}])
        class Store:
            top = str(self.root)
            def env(self):
                return dict(os.environ)
            def git(self, *args, **kwargs):
                check = kwargs.pop("check", True)
                p = subprocess.run(["git", "-C", self.top, *args], capture_output=True, text=True)
                if check and p.returncode:
                    raise RuntimeError(p.stderr)
                return p.stdout
        suspended = baton._suspend_memory_after_rollback(project, "task", Store(), target, later)
        self.assertEqual(suspended, ["M001"])
        self.assertEqual(MemoryLedger(project).latest()["M001"]["status"], "suspended")


if __name__ == "__main__":
    unittest.main()
