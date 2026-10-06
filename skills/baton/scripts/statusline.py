#!/usr/bin/env python3
"""Baton statusline: the statusline the project had before, plus one line for Codex quota.

`baton init` installs this script as the project's statusLine (.claude/settings.local.json).
The first line comes from the statusline that setting overrides, resolved in this order:
$BATON_BASE_STATUSLINE (empty string = none), the project-local statusLine saved by
`baton init` (.baton/statusline-saved.json), the project's .claude/settings.json, and
finally the user's ~/.claude/settings.json. It receives the same stdin JSON.
The Codex line never blocks: it renders cached data and refreshes in the background.
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import codex_quota  # noqa: E402

MARK = "--baton-statusline"
GUARD = "BATON_STATUSLINE_ACTIVE"


def c(text, code):
    return f"\033[{code}m{text}\033[0m"


def is_baton_command(cmd):
    return bool(cmd) and (MARK in cmd or "baton/scripts/statusline.py" in cmd)


def _statusline_command(path):
    try:
        with open(path) as f:
            sl = json.load(f).get("statusLine")
    except Exception:
        return None
    if isinstance(sl, dict) and sl.get("type", "command") == "command":
        return sl.get("command") or None
    return None


def user_settings_path():
    return os.path.join(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")), "settings.json")


def resolve_base(project_dir):
    """The statusline command Baton's project-local setting overrides (None = no base line)."""
    env = os.environ.get("BATON_BASE_STATUSLINE")
    if env is not None:
        return env.strip() or None
    candidates = []
    if project_dir:
        try:
            with open(os.path.join(project_dir, ".baton", "statusline-saved.json")) as f:
                prev = json.load(f).get("previous")
            if isinstance(prev, dict):
                candidates.append(prev.get("command"))
        except Exception:
            pass
        candidates.append(_statusline_command(os.path.join(project_dir, ".claude", "settings.json")))
    candidates.append(_statusline_command(user_settings_path()))
    for cmd in candidates:
        if cmd and not is_baton_command(cmd):
            return cmd
    return None


def project_dir_of(session):
    ws = session.get("workspace") or {}
    return ws.get("project_dir") or ws.get("current_dir") or session.get("cwd") or os.getcwd()


def executor_config(session):
    cfg = codex_quota.skill_config().get("executor", {})
    try:
        with open(os.path.join(project_dir_of(session), ".baton", "config.json")) as f:
            cfg = {**cfg, **(json.load(f).get("executor") or {})}
    except Exception:
        pass
    return cfg


def bar(remaining, width=10):
    filled = max(0, min(width, round(remaining / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def short_reset(ts):
    if not ts:
        return "?"
    left = max(0, int(ts - time.time()))
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    return f"{d}d{h}h" if d else f"{h}h{rem // 60}m"


def codex_line(session):
    ex = executor_config(session)
    head = c(f"Codex {ex.get('model', '?')}·{ex.get('reasoning_effort', '?')}", "36")
    data = codex_quota.get(block=False)
    if not data or not data.get("windows"):
        return f"{head} │ {c('额度获取中…', '2')}"
    threshold = codex_quota.warn_threshold()
    parts = []
    for w in data["windows"]:
        if w.get("reset"):
            parts.append(c(f"{w['label']} 已重置", "2"))
            continue
        rem = w["remaining"]
        color = "31;1" if rem < threshold else ("33" if rem < 30 else "32")
        parts.append(f"{w['label']} 剩余 {c(f'{rem:.0f}%', color)} {c(bar(rem), color)} ↻{short_reset(w['resets_at'])}")
    cr = data.get("credits") or {}
    if cr.get("unlimited"):
        parts.append("credits ∞")
    elif cr.get("has") and cr.get("balance") not in (None, "", "0"):
        parts.append(f"credits {cr['balance']}")
    if data.get("reset_credits"):
        parts.append(f"重置券 {data['reset_credits']}")
    age = int((time.time() - data.get("observed_at", time.time())) / 60)
    if data.get("source") == "rollout":
        parts.append(c(f"取自 {age}m 前的会话", "2"))
    elif age >= 10:
        parts.append(c(f"{age}m 前", "2"))
    line = f"{head} │ " + " │ ".join(parts)
    if codex_quota.is_low(data, threshold):
        line = c(f"Codex 额度低于 {threshold:g}%", "41;97;1") + " " + line
    return line


def main():
    if os.environ.get(GUARD):
        return  # invoked from inside our own base command: a resolution loop, print nothing
    raw = sys.stdin.buffer.read()
    try:
        session = json.loads(raw or b"{}")
    except ValueError:
        session = {}
    lines = []
    cmd = resolve_base(project_dir_of(session))
    if cmd:
        try:
            p = subprocess.run(cmd, shell=True, input=raw, capture_output=True, timeout=8,
                               env=dict(os.environ, **{GUARD: "1"}))
            out = p.stdout.decode("utf-8", "replace").rstrip("\n")
            if out:
                lines.append(out)
        except Exception:
            pass
    try:
        lines.append(codex_line(session))
    except Exception:
        lines.append("Codex 额度：读取失败")
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
