#!/usr/bin/env python3
"""Baton v3 research-core helpers:
- Deterministic 0-token diff RED_FLAGS scanner for scientific/research integrity
- Claude Code (`claude -p --output-format stream-json`) stream parser & L1 watchdog
- Antigravity (`agy -p`) isolated read-only settings & JSON output parser
- Advisor (`baton advise`) schema validation, prompt packing, and Markdown rendering
"""
import json
import os
import re
import time


# ---- 1. Deterministic 0-token RED_FLAGS diff scanner ----

_RED_FLAG_PATTERNS = [
    (
        "silent_exception",
        "高",
        "新增静默吞异常（except/pass 或吞错回退）",
        re.compile(r"\bexcept\b.*:\s*(?:pass\b|\.\.\.)|\bexcept\s*:\s*$"),
    ),
    (
        "hardcoded_truncation",
        "高",
        "新增疑似调试截断或硬编码样本上限（[:N] / max_samples / limit / nrows / head）",
        re.compile(
            r"\[\s*:\s*(?:[1-9]\d{0,3})\s*\]|\b(?:max_samples|max_examples|max_items|sample_limit|nrows)\s*=\s*\d+|\.head\(\s*\d+\s*\)"
        ),
    ),
    (
        "test_skip_or_xfail",
        "高",
        "新增跳过测试或 xfail 标记",
        re.compile(r"@pytest\.mark\.(?:skip|skipif|xfail)|@unittest\.skip|\bpytest\.skip\(|\bpytest\.xfail\("),
    ),
    (
        "tolerance_or_metric_relax",
        "中",
        "修改容差、阈值或评测指标配置（atol/rtol/threshold/tolerance）",
        re.compile(r"\b(?:atol|rtol|tol|tolerance|threshold)\s*=\s*[0-9eE.+-]+"),
    ),
    (
        "seed_or_split_change",
        "中",
        "修改随机种子或数据划分参数（seed/random_state/test_size/val_size）",
        re.compile(r"\b(?:seed|random_state|test_size|val_size|split_ratio)\s*=\s*[0-9.]+"),
    ),
    (
        "nan_or_error_drop",
        "中",
        "新增 NaN/异常样本静默丢弃或屏蔽（dropna/nan_to_num/ignore_errors）",
        re.compile(r"\.dropna\(|\bnan_to_num\(|\bignore_errors\s*=\s*True|\berrors\s*=\s*['\"]ignore['\"]"),
    ),
]


def scan_diff_red_flags(diff_text, max_items=30):
    """Deterministically scan unified diff added lines for research integrity red flags.

    Returns a list of dicts: {rule, severity, summary, file, line, excerpt}.
    """
    if not diff_text:
        return []
    findings = []
    seen = set()
    current_file = ""
    new_line_no = 0
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
    for raw in str(diff_text).splitlines():
        if raw.startswith("+++ b/"):
            current_file = raw[6:].strip()
            continue
        if raw.startswith("+++ "):
            current_file = raw[4:].strip()
            continue
        m = hunk_re.match(raw)
        if m:
            new_line_no = int(m.group(1))
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added = raw[1:]
            stripped = added.strip()
            # Ignore pure comments
            if stripped and not stripped.startswith("#") and not stripped.startswith("//"):
                for rule, severity, summary, pattern in _RED_FLAG_PATTERNS:
                    if pattern.search(added):
                        key = (rule, current_file, new_line_no)
                        if key not in seen:
                            seen.add(key)
                            findings.append({
                                "rule": rule,
                                "severity": severity,
                                "summary": summary,
                                "file": current_file or "(unknown)",
                                "line": new_line_no,
                                "excerpt": stripped[:180],
                            })
                            if len(findings) >= max_items:
                                return findings
            new_line_no += 1
        elif not raw.startswith("-"):
            new_line_no += 1
    return findings


def format_red_flags_section(flags):
    """Format RED_FLAGS markdown block placed at the top of evidence.md."""
    if not flags:
        return "## RED_FLAGS（0-token 静态扫描）\n无命中"
    lines = [
        f"## RED_FLAGS（0-token 静态扫描：命中 {len(flags)} 处，冷眼审计须逐条核实）",
    ]
    for idx, item in enumerate(flags, 1):
        lines.append(
            f"- RF{idx} [{item['severity']}] `{item['rule']}` @ `{item['file']}:{item['line']}` — "
            f"{item['summary']}：`{item['excerpt']}`"
        )
    return "\n".join(lines)


# ---- 2. Claude Code stream-json parser & L1 watchdog ----

def parse_claude_stream(path, offset=0):
    """Parse a Claude Code `--output-format stream-json --verbose` NDJSON file from offset.

    Returns dict with:
      session_id, model, structured_output, result_text, is_error, subtype,
      total_cost_usd, num_turns, usage, model_usage, permission_denials,
      rate_limits, tool_calls, tool_errors, compaction_events, has_output,
      raw_result_event, next_offset
    """
    info = {
        "session_id": None,
        "model": None,
        "structured_output": None,
        "result_text": "",
        "is_error": False,
        "subtype": "",
        "total_cost_usd": None,
        "num_turns": 0,
        "usage": {},
        "model_usage": {},
        "permission_denials": [],
        "rate_limits": None,
        "tool_calls": [],
        "tool_errors": [],
        "compaction_events": 0,
        "has_output": False,
        "raw_result_event": None,
        "next_offset": offset,
    }
    if not path or not os.path.exists(path):
        return info
    pending_tools = {}
    try:
        with open(path, "rb") as f:
            if offset:
                f.seek(offset)
            data = f.read()
            info["next_offset"] = f.tell()
    except OSError:
        return info

    for raw_line in data.splitlines():
        line = raw_line.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid:
            info["session_id"] = sid
        etype = ev.get("type")
        if etype == "system":
            if ev.get("subtype") == "init":
                if isinstance(ev.get("model"), str) and ev["model"]:
                    info["model"] = ev["model"]
            elif "compact" in str(ev.get("subtype") or "").lower():
                info["compaction_events"] += 1
        elif etype == "assistant":
            info["has_output"] = True
            msg = ev.get("message") or {}
            if isinstance(msg, dict):
                if isinstance(msg.get("model"), str) and msg["model"]:
                    info["model"] = msg["model"]
                ts = ev.get("timestamp") or ""
                for block in msg.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tid = str(block.get("id") or "")
                        tname = str(block.get("name") or "")
                        tinput = block.get("input") or {}
                        entry = {
                            "id": tid,
                            "name": tname,
                            "command": tinput.get("command", "") if isinstance(tinput, dict) and tname == "Bash" else "",
                            "input": tinput if isinstance(tinput, dict) else {},
                            "time": ts,
                            "exit_code": None,
                            "is_error": False,
                        }
                        if tid:
                            pending_tools[tid] = entry
                        info["tool_calls"].append(entry)
        elif etype == "user":
            msg = ev.get("message") or {}
            if isinstance(msg, dict):
                for block in msg.get("content") or []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    tid = str(block.get("tool_use_id") or "")
                    is_err = bool(block.get("is_error"))
                    entry = pending_tools.get(tid)
                    if entry is not None:
                        entry["is_error"] = is_err
                        entry["exit_code"] = 1 if is_err else 0
                    if is_err:
                        content = block.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
                        info["tool_errors"].append({
                            "tool": entry["name"] if entry else "",
                            "command": entry["command"] if entry else "",
                            "error": str(content)[:400],
                        })
        elif etype == "rate_limit_event":
            rl = ev.get("rate_limit_info")
            if isinstance(rl, dict):
                info["rate_limits"] = rl
        elif etype == "result":
            info["has_output"] = True
            info["raw_result_event"] = ev
            info["is_error"] = bool(ev.get("is_error"))
            info["subtype"] = str(ev.get("subtype") or "")
            info["total_cost_usd"] = ev.get("total_cost_usd")
            info["num_turns"] = int(ev.get("num_turns") or 0)
            if isinstance(ev.get("usage"), dict):
                info["usage"] = ev["usage"]
            if isinstance(ev.get("modelUsage"), dict):
                info["model_usage"] = ev["modelUsage"]
                if not info["model"] and ev["modelUsage"]:
                    info["model"] = next(iter(ev["modelUsage"].keys()))
            if isinstance(ev.get("permission_denials"), list):
                info["permission_denials"] = ev["permission_denials"]
            if isinstance(ev.get("structured_output"), dict):
                info["structured_output"] = ev["structured_output"]
            if isinstance(ev.get("result"), str):
                info["result_text"] = ev["result"]
                if info["structured_output"] is None:
                    text = ev["result"].strip()
                    if text.startswith("{") and text.endswith("}"):
                        try:
                            parsed = json.loads(text)
                            if isinstance(parsed, dict):
                                info["structured_output"] = parsed
                        except ValueError:
                            pass
    return info


def claude_terminal_calls(events_path, offset=0):
    """Return Bash tool calls from Claude stream-json in Baton's standard call shape."""
    parsed = parse_claude_stream(events_path, offset=offset)
    out = []
    for call in parsed["tool_calls"]:
        if call["name"] == "Bash" and call["command"]:
            out.append({
                "command": call["command"],
                "exit_code": call["exit_code"],
                "time": call["time"] or "",
            })
    return out


def claude_l1_stream_alerts(parsed, repeat_fail=3):
    """Detect 0-token L1 watchdog signals from Claude stream-json."""
    alerts = []
    denials = parsed.get("permission_denials") or []
    if denials:
        alerts.append({
            "rule": "claude_permission_denied",
            "category": "stall",
            "evidence": f"Claude executor 遭遇 {len(denials)} 次权限拒绝",
            "key": f"claude_perm_denied:{len(denials)}",
        })
    # Consecutive failures of the same command/tool
    streak = 0
    last_sig = None
    for err in parsed.get("tool_errors") or []:
        sig = (err.get("tool"), (err.get("command") or "")[:120])
        if sig == last_sig:
            streak += 1
        else:
            last_sig = sig
            streak = 1
        if streak >= repeat_fail:
            alerts.append({
                "rule": "claude_repeat_tool_error",
                "category": "stall",
                "evidence": f"Claude 连续 {streak} 次工具失败：{sig[0]} {sig[1]}",
                "key": f"claude_repeat_err:{sig[0]}:{sig[1]}:{streak}",
            })
    if int(parsed.get("compaction_events") or 0) >= 2:
        alerts.append({
            "rule": "claude_compaction_burst",
            "category": "stall",
            "evidence": f"本轮触发 {parsed['compaction_events']} 次上下文压缩，建议下一轮换新会话",
            "key": f"claude_compact:{parsed['compaction_events']}",
        })
    return alerts


def synthesize_interrupted_claude_result(reason, parsed, diff_stat="", log_tail=""):
    """Synthesize a structured leg-result when Claude executor is stopped/interrupted before emitting result."""
    tools_n = len(parsed.get("tool_calls") or [])
    errs_n = len(parsed.get("tool_errors") or [])
    parts = [f"本轮因 {reason or 'interrupted'} 中断（Claude stream-json 捕获 {tools_n} 次工具调用，{errs_n} 次报错）。"]
    if diff_stat:
        parts.append("改动摘要：" + diff_stat.strip()[:400])
    if log_tail:
        parts.append("log 尾迹：" + log_tail.strip()[-300:])
    return {
        "status": "blocked",
        "summary": " ".join(parts)[:2000],
        "artifact": "",
        "change_size": "minor" if diff_stat else "none",
        "memory_used": [],
        "interrupted": True,
    }


# ---- 3. Antigravity (`agy -p`) isolated read-only helpers ----

def write_isolated_agy_settings(path):
    """Write a minimal read-only session settings JSON for `agy --settings`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "agentMode": "plan",
        "toolPermission": "strict",
        "enableTerminalSandbox": True,
        "allowNonWorkspaceAccess": False,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return path


def parse_agy_json_output(stdout_text):
    """Extract structured JSON object and metadata from `agy --output-format json` stdout.

    Returns (value_dict_or_None, meta_dict, error_string).
    """
    text = (stdout_text or "").strip()
    if not text:
        return None, {}, "agy 没有输出内容"
    wrapper = None
    try:
        wrapper = json.loads(text)
    except ValueError:
        # Tolerate leading non-JSON lines if any warning leaked to stdout
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                wrapper = json.loads(text[start:end + 1])
            except ValueError:
                wrapper = None
    if not isinstance(wrapper, dict):
        return None, {}, "agy 输出不是合法 JSON"

    meta = {
        "conversation_id": wrapper.get("conversation_id", ""),
        "status": wrapper.get("status", ""),
        "duration_seconds": wrapper.get("duration_seconds"),
        "num_turns": wrapper.get("num_turns"),
        "usage": wrapper.get("usage") or {},
    }
    status = str(wrapper.get("status") or "").upper()
    if status and status not in {"SUCCESS", "OK"}:
        err_msg = wrapper.get("error") or wrapper.get("response") or f"agy status={status}"
        return None, meta, str(err_msg)[:500]

    so = wrapper.get("structured_output")
    if isinstance(so, dict):
        return so, meta, ""
    resp = wrapper.get("response")
    if isinstance(resp, dict):
        return resp, meta, ""
    if isinstance(resp, str) and resp.strip():
        r = resp.strip()
        try:
            parsed = json.loads(r)
            if isinstance(parsed, dict):
                return parsed, meta, ""
        except ValueError:
            s, e = r.find("{"), r.rfind("}")
            if s >= 0 and e > s:
                try:
                    parsed = json.loads(r[s:e + 1])
                    if isinstance(parsed, dict):
                        return parsed, meta, ""
                except ValueError:
                    pass
    return None, meta, "agy 输出缺少有效的 structured_output JSON"


# ---- 4. Advisor (`baton advise`) rendering & validation ----

ADVISOR_MODES = ("design", "decide", "audit", "unblock")


def validate_advisor_output(mode, value):
    """Validate structured JSON returned by Advisor for design/decide/unblock modes."""
    if not isinstance(value, dict):
        return ["advisor 输出必须是 JSON object"]
    errors = []
    if not isinstance(value.get("summary"), str) or not value["summary"].strip():
        errors.append("缺少非空 summary")
    if mode == "design":
        for key in ("risks", "missing_invariants", "recommended_freeze", "stop_criteria", "suggestions"):
            if not isinstance(value.get(key), list):
                errors.append(f"design 输出缺少数组字段 {key}")
        if value.get("readiness") not in {"ready", "revise_brief", "need_user_input"}:
            errors.append("design.readiness 必须是 ready / revise_brief / need_user_input")
    elif mode == "decide":
        if not isinstance(value.get("recommended_option"), str) or not value["recommended_option"].strip():
            errors.append("decide 缺少 recommended_option")
        if not isinstance(value.get("tradeoffs"), list):
            errors.append("decide 缺少数组字段 tradeoffs")
        if not isinstance(value.get("draft_precedent"), str) or not value["draft_precedent"].strip():
            errors.append("decide 缺少 draft_precedent")
    elif mode == "unblock":
        if not isinstance(value.get("root_cause_hypotheses"), list) or not value["root_cause_hypotheses"]:
            errors.append("unblock 缺少非空 root_cause_hypotheses")
        if not isinstance(value.get("next_diagnostic_steps"), list) or not value["next_diagnostic_steps"]:
            errors.append("unblock 缺少非空 next_diagnostic_steps")
    return errors


def render_advice_markdown(advice_id, mode, task, model, value, meta=None, context_files=None):
    """Render Advisor JSON into a readable Markdown record under .baton/advice/."""
    meta = meta or {}
    lines = [
        f"# Baton Advisor 意见 · {advice_id}",
        "",
        f"- 任务：`{task}`",
        f"- 模式：`{mode}`",
        f"- 模型：`{model}`",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if meta.get("duration_seconds") is not None:
        lines.append(f"- 耗时：{float(meta['duration_seconds']):.1f}s")
    if context_files:
        lines.append("- 输入上下文：" + "、".join(f"`{p}`" for p in context_files))
    lines += ["", "## 摘要", str(value.get("summary") or "").strip(), ""]

    if mode == "design":
        lines += [f"**开题就绪度**：`{value.get('readiness')}`", ""]
        for title, key in (
            ("设计风险与科学漏洞", "risks"),
            ("缺失不变量（防泄露/评测口径）", "missing_invariants"),
            ("建议冻结的文件清单", "recommended_freeze"),
            ("止损与转向标准", "stop_criteria"),
            ("具体修改建议", "suggestions"),
        ):
            lines.append(f"## {title}")
            items = value.get(key) or []
            if items:
                lines.extend(f"- {item}" for item in items)
            else:
                lines.append("- 无")
            lines.append("")
    elif mode == "decide":
        lines += [
            "## 推荐方案",
            str(value.get("recommended_option") or ""),
            "",
            "## 权衡分析",
        ]
        for item in value.get("tradeoffs") or []:
            lines.append(f"- {item}")
        lines += [
            "",
            "## 拟入账判例草案",
            str(value.get("draft_precedent") or ""),
            "",
        ]
    elif mode == "unblock":
        lines.append("## 根因假设")
        for item in value.get("root_cause_hypotheses") or []:
            lines.append(f"- {item}")
        lines += ["", "## 下一轮最小诊断步骤"]
        for item in value.get("next_diagnostic_steps") or []:
            lines.append(f"- {item}")
        if value.get("should_reset_session"):
            lines += ["", "> Advisor 建议下一轮重置 Executor 会话（新会话 + 压缩交接包）。"]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
