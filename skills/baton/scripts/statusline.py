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
import hashlib
import os
import shutil
import subprocess
import sys
import time

import baton_platform as platform_support

platform_support.utf8_stdio()

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
        with open(path, encoding="utf-8") as f:
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
            with open(os.path.join(project_dir, ".baton", "statusline-saved.json"), encoding="utf-8") as f:
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


def project_baton_json(session):
    """The project's root baton.json (v2 settings), or {}."""
    try:
        with open(os.path.join(project_dir_of(session), "baton.json")) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def executor_config(session):
    cfg = codex_quota.skill_config().get("executor", {})
    try:
        with open(os.path.join(project_dir_of(session), ".baton", "config.json"), encoding="utf-8") as f:
            cfg = {**cfg, **(json.load(f).get("executor") or {})}
    except Exception:
        pass
    cfg = {**cfg, **(project_baton_json(session).get("executor") or {})}
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


def _project_id(project_dir):
    real = os.path.realpath(project_dir)
    return os.path.basename(real) + "-" + hashlib.sha1(os.fsencode(real)).hexdigest()[:8]


def _running_round(session):
    """Read only the small Baton state needed for the optional runtime line."""
    project_dir = project_dir_of(session)
    candidates = []
    local = os.path.join(project_dir, ".baton", "tasks")
    candidates.append(local)
    home = os.environ.get("BATON_HOME") or "~/.baton"
    if "${" not in home:
        home = os.path.abspath(os.path.expanduser(home))
        candidates.append(os.path.join(home, "projects", _project_id(project_dir), "tasks"))
    for tasks_dir in candidates:
        try:
            names = sorted(os.listdir(tasks_dir))
        except OSError:
            continue
        for task in names:
            path = os.path.join(tasks_dir, task, "state.json")
            try:
                with open(path, encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, ValueError):
                continue
            if not isinstance(state, dict):
                continue
            legs = state.get("legs") or []
            if not legs:
                continue
            leg = legs[-1]
            monitor = leg.get("monitor") or {}
            if leg.get("status") != "running" and monitor.get("status") != "running":
                continue
            model = leg.get("model") or (leg.get("executor") or {}).get("model") or "?"
            result = {"task": task, "leg": leg.get("n", len(legs)), "model": model,
                      "escalated": bool(leg.get("escalated")),
                      "monitor": monitor.get("status") == "running",
                      "capacity_wait": leg.get("capacity_wait")}
            return result
    return None


def _latest_task(session):
    """The most recently updated task (name, last leg number and status), for the idle line."""
    project_dir = project_dir_of(session)
    best = None
    for tasks_dir in (os.path.join(project_dir, ".baton", "tasks"),):
        try:
            names = os.listdir(tasks_dir)
        except OSError:
            continue
        for task in names:
            path = os.path.join(tasks_dir, task, "state.json")
            try:
                mtime = os.path.getmtime(path)
                with open(path, encoding="utf-8") as stream:
                    state = json.load(stream)
            except (OSError, ValueError):
                continue
            if not isinstance(state, dict):
                continue
            legs = state.get("legs") or []
            leg = legs[-1] if legs else {}
            item = (mtime, task, leg.get("n", len(legs)), leg.get("status") or state.get("status") or "?")
            if best is None or item[0] > best[0]:
                best = item
    return best


def idle_line(session):
    data = project_baton_json(session)
    mode = data.get("mode") or "standard"
    head = f"Baton v2·{mode}" + (f"·{data['preset']}" if isinstance(data.get("preset"), str) and data["preset"] else "")
    parts = [head, "空闲"]
    latest = _latest_task(session)
    if latest:
        parts.append(f"最近 {latest[1]}·第{latest[2]}轮·{latest[3]}")
    return " │ ".join(parts)


def runtime_line(session):
    info = _running_round(session)
    if not info:
        return idle_line(session)
    model = str(info["model"]) + ("（升级）" if info.get("escalated") else "")
    parts = [f"Baton {info['task']}·第{info['leg']}轮·{model}",
             "monitor 运行中" if info.get("monitor") else "monitor 未运行"]
    wait = info.get("capacity_wait")
    if isinstance(wait, dict):
        remaining = max(0, int(float(wait.get("next_retry_at", time.time())) - time.time()))
        parts.append(f"容量重试等待 {remaining // 60}m")
    return " │ ".join(parts)


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
            # Match Claude Code's Git Bash statusLine command semantics on Windows.
            command = [shutil.which("bash.exe") or "bash.exe", "-c", cmd] if os.name == "nt" else cmd
            p = subprocess.run(command, shell=os.name != "nt", input=raw, capture_output=True, timeout=8,
                               env=dict(os.environ, **{GUARD: "1"}))
            out = p.stdout.decode("utf-8", "replace").rstrip("\n")
            if out:
                lines.append(out)
        except Exception as e:
            if os.name == "nt":
                print(f"[fail] Base statusline: {e}; rendering quota only", file=sys.stderr)
    try:
        lines.append(codex_line(session))
    except Exception:
        lines.append("Codex 额度：读取失败")
    try:
        runtime = runtime_line(session)
        if runtime:
            lines.append(c(runtime, "35"))
    except Exception:
        pass
    sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
