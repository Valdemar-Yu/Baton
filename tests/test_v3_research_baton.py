#!/usr/bin/env python3
"""Unit tests for Baton v3 (Agy-core + Claude-coding Research Baton) and agy statusline."""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "skills", "baton", "scripts")
sys.path.insert(0, SCRIPTS)

import agy_statusline  # noqa: E402
import baton  # noqa: E402
import v3_research  # noqa: E402


class _FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0, pid=42424):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = pid

    def communicate(self, input=None, timeout=None):
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def wait(self):
        return self.returncode


class V3ResearchUnitTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="baton-v3-test-")
        self.project_root = os.path.join(self._tmp, "proj")
        self.baton_home = os.path.join(self._tmp, "baton-home")
        os.makedirs(self.project_root, exist_ok=True)
        self._old_home = os.environ.get("BATON_HOME")
        os.environ["BATON_HOME"] = self.baton_home

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("BATON_HOME", None)
        else:
            os.environ["BATON_HOME"] = self._old_home
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _init_project(self):
        project = baton.Project(self.project_root)
        args = argparse.Namespace(
            no_statusline=True, accept_boundary=True, relink=False,
            fresh=False, upgrade_profile=False, migrate=False,
        )
        with redirect_stdout(io.StringIO()):
            baton.cmd_init(project, args)
        return baton.Project(self.project_root)

    # ---- 1. Default v3 config & presets ----
    def test_v3_default_config_and_init_presets(self):
        project = self._init_project()
        cfg = project.config
        self.assertEqual(cfg.get("version"), 3)
        self.assertEqual(cfg["conductor"]["model"], "gemini-4-carbon-high")
        self.assertTrue(cfg["advisor"]["enabled"])
        self.assertEqual(cfg["advisor"]["model"], "gemini-4-neon-xhigh")
        self.assertEqual(cfg["monitor"]["backend"], "agy")
        self.assertEqual(cfg["monitor"]["model"], "gemini-4-hydrogen-xhigh")
        self.assertFalse(cfg["monitor"]["midleg_llm_checks"])
        self.assertEqual(cfg["executor"]["backend"], "agy")
        self.assertEqual(cfg["executor"]["model"], "claude-opus-5-5-high")
        self.assertEqual(cfg["executor"]["reasoning_effort"], "high")
        self.assertEqual(cfg["executor"]["claude_bin"], "claude-00")

        # Switch preset to research-conserve and verify
        with redirect_stdout(io.StringIO()):
            baton.cmd_preset(project, argparse.Namespace(action="use", name="research-conserve", task=[]))
        cfg2 = baton.Project(self.project_root).config
        self.assertEqual(cfg2["monitor"]["model"], "gemini-4-hydrogen-high")
        self.assertEqual(cfg2["advisor"]["model"], "gemini-4-neon-high")
        self.assertEqual(cfg2["executor"]["backend"], "agy")

        # Switch presets to claude-00 / claude-01 / claude-02 (claude-accounts)
        for slug in ("claude-00", "claude-01", "claude-02"):
            with redirect_stdout(io.StringIO()):
                baton.cmd_preset(project, argparse.Namespace(action="use", name=slug, task=[]))
            cfg_c = baton.Project(self.project_root).config
            self.assertEqual(cfg_c["executor"]["backend"], "claude")
            self.assertEqual(cfg_c["executor"]["claude_bin"], slug)
            self.assertEqual(cfg_c["executor"]["model"], "opus")
            self.assertEqual(cfg_c["executor"]["reasoning_effort"], "xhigh")

        # Switch preset to v2-codex and verify v2 compatibility
        with redirect_stdout(io.StringIO()):
            baton.cmd_preset(project, argparse.Namespace(action="use", name="v2-codex", task=[]))
        cfg3 = baton.Project(self.project_root).config
        self.assertEqual(cfg3["conductor"]["model"], "gpt-6-astra")
        self.assertFalse(cfg3["advisor"]["enabled"])
        self.assertEqual(cfg3["executor"]["backend"], "codex")
        self.assertEqual(cfg3["monitor"]["backend"], "codex")

    # ---- 2. Deterministic 0-token RED_FLAGS diff scanner ----
    def test_red_flags_diff_scanner(self):
        diff = "\n".join([
            "diff --git a/src/train.py b/src/train.py",
            "--- a/src/train.py",
            "+++ b/src/train.py",
            "@@ -10,2 +10,9 @@",
            " def run(df):",
            "+    # except: pass comment should be ignored",
            "+    df = df[:100]",
            "+    df = df.dropna()",
            "+    try:",
            "+        score = eval_model(df, seed=7, atol=1e-2)",
            "+    except Exception: pass",
            "diff --git a/tests/test_eval.py b/tests/test_eval.py",
            "+++ b/tests/test_eval.py",
            "@@ -1,1 +1,2 @@",
            "+@pytest.mark.skip(reason='flaky')",
        ])
        flags = v3_research.scan_diff_red_flags(diff)
        rules = {f["rule"] for f in flags}
        self.assertIn("hardcoded_truncation", rules)
        self.assertIn("nan_or_error_drop", rules)
        self.assertIn("silent_exception", rules)
        self.assertIn("seed_or_split_change", rules)
        self.assertIn("tolerance_or_metric_relax", rules)
        self.assertIn("test_skip_or_xfail", rules)
        block = v3_research.format_red_flags_section(flags)
        self.assertIn("RED_FLAGS", block)
        self.assertIn("RF1", block)

    # ---- 3. Claude stream-json parser & L1 watchdog ----
    def test_claude_stream_parser_and_l1_alerts(self):
        stream_path = os.path.join(self._tmp, "events.jsonl")
        events = [
            {"type": "system", "subtype": "init", "session_id": "sid-123", "model": "claude-opus-5-5"},
            {"type": "assistant", "timestamp": "2026-11-19T10:00:00Z", "message": {
                "model": "claude-opus-5-5",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}],
            }},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "fail"}]}},
            {"type": "assistant", "timestamp": "2026-11-19T10:01:00Z", "message": {
                "content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}}],
            }},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "fail"}]}},
            {"type": "assistant", "timestamp": "2026-11-19T10:02:00Z", "message": {
                "content": [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "pytest -q"}}],
            }},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "fail"}]}},
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "result", "subtype": "success", "is_error": False, "session_id": "sid-123",
             "total_cost_usd": 0.1234, "num_turns": 4,
             "permission_denials": [{"tool": "Bash"}],
             "structured_output": {"status": "done", "summary": "all green", "artifact": "", "change_size": "minor", "memory_used": []}},
        ]
        with open(stream_path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

        parsed = v3_research.parse_claude_stream(stream_path)
        self.assertEqual(parsed["session_id"], "sid-123")
        self.assertEqual(parsed["model"], "claude-opus-5-5")
        self.assertEqual(parsed["structured_output"]["status"], "done")
        self.assertEqual(len(parsed["tool_calls"]), 3)
        calls = v3_research.claude_terminal_calls(stream_path)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["command"], "pytest -q")
        self.assertEqual(calls[0]["exit_code"], 1)

        alerts = v3_research.claude_l1_stream_alerts(parsed, repeat_fail=3)
        alert_rules = {a["rule"] for a in alerts}
        self.assertIn("claude_permission_denied", alert_rules)
        self.assertIn("claude_repeat_tool_error", alert_rules)
        self.assertIn("claude_compaction_burst", alert_rules)

        synth = v3_research.synthesize_interrupted_claude_result("stopped", parsed, diff_stat="1 file changed")
        self.assertEqual(synth["status"], "blocked")
        self.assertTrue(synth["interrupted"])

    # ---- 4. Claude executor command (with claude-00/01/02) & session reset rules ----
    def test_claude_command_and_session_reset(self):
        project = self._init_project()
        with redirect_stdout(io.StringIO()):
            baton.cmd_preset(project, argparse.Namespace(action="use", name="claude-01", task=[]))
        cfg = baton.Project(self.project_root).config
        cmd = baton.claude_command(
            project, "start", None, cfg=cfg, task="exp1", leg_n=1,
            system_prompt_text="approved harness", session_id="uuid-1", reset_session=True,
        )
        self.assertTrue(cmd[0].endswith("claude-01"))
        self.assertIn("--safe-mode", cmd)
        self.assertNotIn("--bare", cmd)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "xhigh")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[cmd.index("--permission-prompts") + 1], "none")
        self.assertIn("--disallowedTools", cmd)
        self.assertIn("Bash(baton *)", cmd)
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "uuid-1")
        self.assertIn("--append-system-prompt", cmd)

        # Resume unchanged -> --resume
        cmd_res = baton.claude_command(
            project, "resume", "uuid-1", cfg=cfg, task="exp1", leg_n=2,
            system_prompt_text="approved harness", reset_session=False,
        )
        self.assertIn("--resume", cmd_res)
        self.assertEqual(cmd_res[cmd_res.index("--resume") + 1], "uuid-1")

        # Reset triggers (including switching backend from agy -> claude!)
        st = {"thread_id": "uuid-1", "claude_session_harness_sha256": "aaa", "claude_session_leg_count": 1,
              "legs": [{"status": "report", "backend": "claude"}]}
        self.assertEqual(baton._claude_should_reset_session(st, "aaa", cfg)[0], False)
        self.assertEqual(baton._claude_should_reset_session(st, "bbb", cfg), (True, "harness_or_charter_changed"))
        st_switch = dict(st, legs=[{"status": "report", "backend": "agy"}])
        self.assertEqual(baton._claude_should_reset_session(st_switch, "aaa", cfg), (True, "backend_switched"))
        st_viol = dict(st, legs=[{"status": "violation", "backend": "claude"}])
        self.assertEqual(baton._claude_should_reset_session(st_viol, "aaa", cfg), (True, "after_violation"))
        st_rb = dict(st, rolled_back_since_leg=True)
        self.assertEqual(baton._claude_should_reset_session(st_rb, "aaa", cfg), (True, "after_rollback"))
        st_max = dict(st, claude_session_leg_count=4)
        self.assertEqual(baton._claude_should_reset_session(st_max, "aaa", cfg)[0], True)

    # ---- 4b. Agy Opus executor command & stream-json parser ----
    def test_agy_executor_command_and_stream_parser(self):
        project = self._init_project()
        cfg = project.config
        cmd = baton.agy_executor_command(project, "start", None, "implement feature", cfg=cfg, reset_session=True)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-5-5-high")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--json-schema", cmd)
        self.assertNotIn("--conversation", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "implement feature")

        cmd_res = baton.agy_executor_command(project, "resume", "conv-42", "next step", cfg=cfg, reset_session=False)
        self.assertEqual(cmd_res[cmd_res.index("--conversation") + 1], "conv-42")

        stream_path = os.path.join(self._tmp, "agy-events.jsonl")
        lines = [
            {"event": "init", "conversation_id": "conv-42", "init": {"model": "claude-opus-5-5-high"}},
            {"event": "step_update", "step_update": {"conversation_id": "conv-42", "step_type": "run_command", "command": "pytest -q", "exit_code": 0}},
            {"event": "result", "result": {
                "conversation_id": "conv-42", "status": "SUCCESS", "duration_seconds": 6.4, "num_turns": 2,
                "usage": {"input_tokens": 12000, "output_tokens": 300},
                "structured_output": {"status": "done", "summary": "implemented", "artifact": "", "change_size": "minor", "memory_used": []},
            }},
        ]
        with open(stream_path, "w", encoding="utf-8") as f:
            for item in lines:
                f.write(json.dumps(item) + "\n")
        parsed = v3_research.parse_agy_stream(stream_path)
        self.assertEqual(parsed["conversation_id"], "conv-42")
        self.assertEqual(parsed["model"], "claude-opus-5-5-high")
        self.assertEqual(parsed["structured_output"]["status"], "done")
        self.assertEqual(len(parsed["tool_calls"]), 1)
        self.assertEqual(parsed["tool_calls"][0]["command"], "pytest -q")

    # ---- 5. Agy isolated command & parser ----
    def test_agy_command_and_json_parser(self):
        project = self._init_project()
        cfg = project.config
        iso = v3_research.write_isolated_agy_settings(os.path.join(self._tmp, "iso.json"))
        cmd = baton._monitor_agy_command(project, "R001", cfg, "audit prompt", settings_path=iso)
        self.assertIn("--sandbox", cmd)
        self.assertIn("--disable-slash-commands", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gemini-4-hydrogen-xhigh")
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "json")
        self.assertIn("--json-schema", cmd)
        self.assertEqual(cmd[cmd.index("-p") + 1], "audit prompt")

        raw_ok = json.dumps({
            "conversation_id": "c1", "status": "SUCCESS", "duration_seconds": 4.2,
            "structured_output": {"summary": "ok", "recommendation": "APPROVE", "findings": [], "memory_advice": []},
        })
        val, meta, err = v3_research.parse_agy_json_output(raw_ok)
        self.assertEqual(err, "")
        self.assertEqual(val["recommendation"], "APPROVE")
        self.assertEqual(meta["conversation_id"], "c1")

    # ---- 6. Single-track audit router ----
    def test_single_track_audit_router(self):
        project = self._init_project()
        cfg = project.config

        # Normal report -> Monitor (Hydrogen)
        role, eff, _ = baton.select_auditor_for_dossier(project, "t1", "report", cfg)
        self.assertEqual(role, "monitor")
        self.assertEqual(eff["monitor"]["model"], "gemini-4-hydrogen-xhigh")

        # Final -> Advisor (Neon) directly
        role, eff, reason = baton.select_auditor_for_dossier(project, "t1", "final", cfg)
        self.assertEqual(role, "advisor")
        self.assertEqual(eff["monitor"]["model"], "gemini-4-neon-xhigh")
        self.assertIn("终审", reason)

        # Violation -> Advisor (Neon) directly
        role, eff, _ = baton.select_auditor_for_dossier(project, "t1", "violation", cfg)
        self.assertEqual(role, "advisor")
        self.assertEqual(eff["monitor"]["model"], "gemini-4-neon-xhigh")

        # After 2 consecutive REVISE verdicts -> report escalates directly to Advisor (Neon)
        baton.append_ledger(project, {"type": "verdict", "task": "t1", "dossier": "R001", "conclusion": "REVISE"})
        baton.append_ledger(project, {"type": "verdict", "task": "t1", "dossier": "R002", "conclusion": "REVISE"})
        role, eff, reason = baton.select_auditor_for_dossier(project, "t1", "report", cfg)
        self.assertEqual(role, "advisor")
        self.assertEqual(eff["monitor"]["model"], "gemini-4-neon-xhigh")
        self.assertIn("REVISE", reason)

        # L1 watchdog: midleg LLM check disabled by default in v3
        st = {"task": "t1", "config_snapshot": cfg, "legs": [{"n": 1, "status": "running", "next_check_at": 0}]}
        self.assertFalse(baton.maybe_start_monitor_check(project, "t1", st, st["legs"][0], force=False))

    # ---- 7. First-class `baton advise design` + ledger integrity ----
    def test_cmd_advise_design_writes_artifacts_and_ledger(self):
        project = self._init_project()
        with redirect_stdout(io.StringIO()):
            baton.cmd_new(project, argparse.Namespace(task="exp1", brief=None))
        fake_resp = json.dumps({
            "conversation_id": "adv-1",
            "status": "SUCCESS",
            "duration_seconds": 3.5,
            "structured_output": {
                "summary": "实验设计整体合理，但需冻结评测集切分脚本并明确数据泄露检查。",
                "readiness": "revise_brief",
                "risks": ["特征标准化若在 train/test split 前执行会导致数据泄露"],
                "missing_invariants": ["scaler 仅在 train fold fit"],
                "recommended_freeze": ["eval/metrics.py"],
                "stop_criteria": ["3 个 seed 均低于 baseline 则停止"],
                "suggestions": ["在 baton-scope.frozen 中加入 eval/metrics.py"],
            },
        })
        adv_args = argparse.Namespace(
            advise_mode="design", task="exp1", decision=None, dossier=None,
            rerun=False, files=[], question="请检查实验防泄露设计",
        )
        out = io.StringIO()
        with mock.patch.object(baton.platform_support, "detached_popen", return_value=_FakeProc(stdout=fake_resp)):
            with redirect_stdout(out):
                baton.cmd_advise(project, adv_args)
        self.assertIn("BATON::ADVICE id=A001 mode=design model=gemini-4-neon-xhigh", out.getvalue())
        self.assertTrue(os.path.isfile(project.p("advice", "A001.json")))
        self.assertTrue(os.path.isfile(project.p("advice", "A001.md")))
        self.assertTrue(baton.verify_ledger(project))
        records = baton.ledger_records(project)
        self.assertEqual(records[-1]["type"], "advice")
        self.assertEqual(records[-1]["advice_id"], "A001")

    # ---- 8. agy_statusline rendering ----
    def test_agy_statusline_rendering_and_baton_home_exclusion(self):
        project = self._init_project()
        payload = {
            "model": {"id": "gemini-4-carbon-high", "display_name": "Gemini 4 Carbon (High)"},
            "context_window": {
                "context_window_size": 1048576,
                "used_percentage": 22.0,
                "current_usage": {"input_tokens": 200000, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 0},
            },
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.85, "reset_in_seconds": 7200},
                "gemini-weekly": {"remaining_fraction": 0.91, "reset_in_seconds": 432000},
                "3p-5h": {"remaining_fraction": 0.99, "reset_in_seconds": 14400},
                "3p-weekly": {"reset_in_seconds": 600000},  # Protobuf v3 zero-value omission -> 0%
            },
            "workspace": {"current_dir": project.root},
            "terminal_width": 160,
        }
        rendered = agy_statusline.render(payload)
        lines = rendered.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("Gemini 4 Carbon [1M]", lines[0])
        self.assertIn("⚡high", lines[0])
        self.assertIn("22%", lines[0])
        self.assertIn("Gemini", lines[0])
        self.assertIn("85%", lines[0])
        self.assertIn("91%", lines[0])
        self.assertIn("Opus/3P", lines[1])
        self.assertIn("99%", lines[1])
        self.assertIn("0%", lines[1])
        self.assertIn("Baton v3", lines[1])
        self.assertIn("agy:claude-opus-5-5-high", lines[1])

        # Narrow terminal splits Baton onto Line 3 instead of wrapping Line 2
        narrow = dict(payload, terminal_width=95)
        narrow_lines = agy_statusline.render(narrow).splitlines()
        self.assertEqual(len(narrow_lines), 3)
        self.assertIn("Opus/3P", narrow_lines[1])
        self.assertIn("Baton v3", narrow_lines[2])

        # Home dir containing only global $BATON_HOME must NOT be treated as a Baton project
        fake_home = os.path.join(self._tmp, "fakehome")
        os.makedirs(os.path.join(fake_home, ".baton"), exist_ok=True)
        with mock.patch.dict(os.environ, {"BATON_HOME": os.path.join(fake_home, ".baton")}):
            self.assertIsNone(agy_statusline._find_baton_root(fake_home))

    # ---- 9. Quota-exhaustion preset switch hints ----
    def test_quota_switch_hints_in_leg_event_text(self):
        project = self._init_project()
        with redirect_stdout(io.StringIO()):
            baton.cmd_new(project, argparse.Namespace(task="q1", brief=None))
        leg_agy = {
            "n": 1,
            "status": "failed",
            "backend": "agy",
            "model": "claude-opus-5-5-high",
            "failure_kind": "quota",
            "started_at": 1000,
            "ended_at": 1010,
            "error": "quota exhausted",
        }
        text_agy = baton.leg_event_text(project, "q1", leg_agy)
        self.assertIn("baton preset use claude-00 --task q1", text_agy)

        def _set_claude(st):
            cfg = baton.config_for_state(project, st)
            cfg["executor"]["backend"] = "claude"
            cfg["executor"]["claude_bin"] = "claude-00"
            st["config_snapshot"] = cfg

        project.update_state("q1", _set_claude)
        leg_claude = dict(leg_agy, backend="claude", model="opus")
        text_claude = baton.leg_event_text(project, "q1", leg_claude)
        self.assertIn("baton preset use claude-01 --task q1", text_claude)
        self.assertIn("claude-02 / research", text_claude)


if __name__ == "__main__":
    unittest.main()
