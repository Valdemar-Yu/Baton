#!/usr/bin/env python3
"""Baton: Claude (conductor / monitor) drives Codex CLI (executor).

Every Codex run is a "leg": `codex exec` (first leg) or `codex exec resume <thread>` (later legs).
A leg ends when the executor finishes, asks for a decision, hands in a major-change report,
gets blocked, or is stopped. Legs run detached, so they survive the Claude session; Claude
blocks on `baton wait` (in the background) and is woken by leg ends and 30-minute ticks.

State lives in <project>/.baton/ (see README). Python 3.9+, stdlib only.
"""
import argparse
import datetime
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import tarfile
import xml.etree.ElementTree as ET

import baton_platform as platform_support

platform_support.utf8_stdio()

try:
    import fcntl
except ImportError:
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
TEMPLATES = os.path.join(SKILL_DIR, "templates")
REFERENCES = os.path.join(SKILL_DIR, "references")
sys.path.insert(0, HERE)
import codex_quota  # noqa: E402
import statusline as baton_statusline  # noqa: E402

# Baton's own records never count as code: not at the top, and not in a nested project either.
EXCLUDE = [":(exclude,glob)**/.baton/**", ":(exclude,glob)**/.council/**"]
# Default info/exclude of the shadow repo, written once when the repo is created (decision D001):
# data/ and models/ are often code packages (src/models/), so only the top-level ones are
# excluded; outputs/, logs/ and checkpoints/ are excluded at any depth.
SHADOW_EXCLUDE_MARK = "# Baton shadow-repo excludes"
SHADOW_EXCLUDE_DEFAULTS = [
    "**/.baton/runs/", "**/.baton/rollback-backup/", "**/.claude/settings.local.json",
    ".venv*/", "venv/", "node_modules/", "__pycache__/", "*.pyc", ".DS_Store",
    "/data/", "/models/", "outputs/", "logs/", "checkpoints/",
]
SCOPE_KEYS = {"allow", "deny", "acceptance", "frozen", "verify"}
SCOPE_LIST_KEYS = {"allow", "deny", "acceptance", "frozen"}
FROZEN_WHOLE_NAMES = {"conftest.py", "pytest.ini", "tox.ini"}
HERMES_NAMES = {".hermes.md", "HERMES.md", "AGENTS.md", "CLAUDE.md", ".cursorrules"}
DEPENDENCY_NAMES = ("requirements", "Pipfile", "poetry.lock", "package-lock.json", "yarn.lock",
                    "Cargo.toml", "go.mod", "Makefile")
# Directory names worth showing at init wherever they occur, so the user can confirm them.
BULKY_DIR_NAMES = {"data", "models", "outputs", "logs", "checkpoints"}
# Every variable `git rev-parse --local-env-vars` lists: none may leak into shadow-repo calls.
GIT_LOCAL_ENV_VARS = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE",
    "GIT_INDEX_FILE", "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
    "GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
)
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
HERMES_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
EXECUTOR_BACKENDS = {"codex", "hermes"}
BATON_GITIGNORE = ["runs/", "*.lock", "statusline-saved.json", "tasks/*/state.json", "rollback-backup/", "project-id"]
_DETACHED_RUNNERS = []
STARTUP_GRACE_SECONDS = 30
LEG_END_EVENTS = {
    "done": "DONE", "decision": "DECISION", "report": "REPORT", "blocked": "BLOCKED",
    "failed": "FAILED", "stopped": "STOPPED", "paused_unsupervised": "PAUSED_UNSUPERVISED",
    "violation": "VIOLATION", "breaking": "BREAKING", "drift_unhandled": "DRIFT_UNHANDLED",
}
NEXT_STEP = {
    "DONE": "读监督简报，用 baton rule <task> <卷宗号> <裁决文件> 裁决，再 baton resume <task> --verdict <卷宗号>。",
    "DECISION": "读决策请求文件，写决定文件后运行 baton decide <task> <D号> <决定文件>，再 baton resume。",
    "REPORT": "读监督简报，用 baton rule <task> <卷宗号> <裁决文件> 裁决，再 baton resume <task> --verdict <卷宗号>。",
    "BLOCKED": "看 summary 里缺什么；能解决就解决后 baton resume，涉及权限/网络/费用先问用户。",
    "FAILED": "看错误和 stderr 末尾判断原因（额度、网络、参数）；修正后 baton resume 重试本轮。",
    "STOPPED": "已按指令停止。写纠偏/修复指令后 baton resume。",
    "PAUSED_UNSUPERVISED": "超过监管时限没有 Opus 检查，执行者已被暂停。先做一次监管检查，再 baton resume。",
    "VIOLATION": "读取监督简报，用 baton rule <task> <卷宗号> <裁决文件> 裁决，再按结论 restore-frozen 或 amend --adopt，最后 baton resume --verdict。",
    "TICK": "按 references/supervision.md 做一次监管检查，结束时运行 baton supervised 记录结论，然后重新后台 baton wait。",
    "QUOTA_LOW": "立刻在回复里提醒用户 Codex 额度不足，然后重新后台 baton wait。",
    "BREAKING": "读取轮中检查简报；本轮已结束，确认回滚点和验证结果后按简报处理。",
    "DRIFT_UNHANDLED": "读取轮中检查简报；未处理的漂移已结束本轮，先 steer 或 dismiss 后再 resume。",
    "DRIFT": "读取轮中检查简报；执行者仍在工作，可用 baton steer <task> 或 baton supervised <task> --dismiss F1 --reason \"…\"。",
}


# ---------------------------------------------------------------- helpers

def now():
    return time.time()


def stamp(ts=None, fmt="%Y-%m-%d %H:%M"):
    return datetime.datetime.fromtimestamp(ts or now()).strftime(fmt)


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def read_json(path, default=None):
    try:
        return json.loads(platform_support.read_text(path))
    except Exception:
        return default


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    platform_support.atomic_replace(tmp, path)


def write_json_keep(path, data):
    """Atomic write that keeps the file's mode and writes through a symlink to its target."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = None
    tmp = f"{real}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others mid-write
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode if mode is not None else 0o644)
    platform_support.replace_readonly(tmp, real)


def read_text(path, default=""):
    try:
        return platform_support.read_text(path)
    except OSError:
        return default


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def read_bytes_since(path, offset):
    """Text appended after byte `offset`; None if the file shrank below it or disappeared."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size < offset:
                return None
            f.seek(offset)
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None if offset > 0 else ""


def proc_cmdline(pid):
    if os.name == "nt":
        return platform_support.proc_cmdline(pid)
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True, encoding="utf-8").stdout.strip()
    except OSError:
        return ""


def pid_alive(pid, marker=None):
    """Process exists (and, with `marker`, its command line contains it: guards PID reuse)."""
    if not pid:
        return False
    if os.name == "nt":
        return platform_support.pid_alive(pid) and (marker is None or marker in proc_cmdline(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return marker is None
    return marker is None or marker in proc_cmdline(pid)


def reap_detached_runners():
    """Let Popen observe exited detached runners so their parent objects do not warn at GC."""
    alive = []
    for proc in _DETACHED_RUNNERS:
        try:
            if proc.poll() is None:
                alive.append(proc)
        except OSError:
            pass
    _DETACHED_RUNNERS[:] = alive


def die(msg, code=1):
    sys.stdout.flush()
    print(f"baton: {msg}", file=sys.stderr)
    sys.exit(code)


class ConfigError(Exception):
    """A project configuration error that should be shown to the caller."""


CONDUCTOR_MODELS = {"opus", "sonnet", "haiku", "fable"}
MODES = {"standard", "lean"}
CAPACITY_RETRY_DEFAULTS = {"immediate": 3, "delayed": 1, "delayed_minutes": 15}


def lean_overrides():
    return {
        "conductor": {"reasoning_effort": "medium"},
        "executor": {
            "model": "gpt-5.6-luna",
            "reasoning_effort": "medium",
            "escalation": {"enabled": False},
        },
        "monitor": {"enabled": False},
        "supervision": {"max_unsupervised_minutes": 0, "interval_minutes": 0},
    }


def validate_config(cfg):
    """Validate an already-merged config, including configs amended in task state."""
    mode = cfg.get("mode", "standard")
    if not isinstance(mode, str) or mode not in MODES:
        raise ConfigError(f"mode 的值 {mode!r} 无效，只能是 standard 或 lean")
    for role in ("conductor", "monitor"):
        section = cfg.get(role)
        if not isinstance(section, dict):
            raise ConfigError(f"配置中的 {role} 必须是 JSON object")
        model = section.get("model")
        if role == "conductor":
            if not isinstance(model, str) or model not in CONDUCTOR_MODELS:
                raise ConfigError(
                    f"conductor.model 的值 {model!r} 无效，只能是 opus、sonnet、haiku 或 fable"
                )
        elif not isinstance(model, str) or not model.strip():
            raise ConfigError("monitor.model 必须是非空字符串")
    if not isinstance(cfg.get("executor"), dict):
        raise ConfigError("配置中的 executor 必须是 JSON object")
    executor = cfg["executor"]
    backend = executor.get("backend", "codex")
    if not isinstance(backend, str) or backend not in EXECUTOR_BACKENDS:
        raise ConfigError(f"executor.backend 的值 {backend!r} 无效，只能是 codex 或 hermes")
    profile = executor.get("hermes_profile", "baton-exec-v2")
    if not isinstance(profile, str) or not HERMES_PROFILE_RE.fullmatch(profile):
        raise ConfigError("executor.hermes_profile 必须是小写字母、数字、下划线或连字符，且以字母或数字开头")
    provider = executor.get("provider", "openai-codex")
    if not isinstance(provider, str) or not provider.strip():
        raise ConfigError("executor.provider 必须是非空字符串")
    toolsets = executor.get("toolsets", ["file", "terminal", "todo"])
    if (not isinstance(toolsets, list) or not toolsets
            or any(not isinstance(item, str) or not item.strip() for item in toolsets)
            or len(set(toolsets)) != len(toolsets)):
        raise ConfigError("executor.toolsets 必须是不重复的非空字符串数组")
    if not isinstance(executor.get("sandbox_exec", True), bool):
        raise ConfigError("executor.sandbox_exec 必须是布尔值")
    escalation = executor.get("escalation", {})
    if not isinstance(escalation, dict):
        raise ConfigError("executor.escalation 必须是 JSON object")
    if not isinstance(escalation.get("enabled", True), bool):
        raise ConfigError("executor.escalation.enabled 必须是布尔值")
    escalation_model = escalation.get("model", executor.get("model"))
    if not isinstance(escalation_model, str) or not escalation_model.strip():
        raise ConfigError("executor.escalation.model 必须是非空字符串")
    escalation_effort = escalation.get("reasoning_effort", executor.get("reasoning_effort"))
    if not isinstance(escalation_effort, str) or not escalation_effort.strip():
        raise ConfigError("executor.escalation.reasoning_effort 必须是非空字符串")
    for key in ("after_revise", "after_blocked_or_failed_legs", "max_per_task"):
        value = escalation.get(key, {"after_revise": 2,
                                     "after_blocked_or_failed_legs": 2,
                                     "max_per_task": 3}[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"executor.escalation.{key} 必须是正整数")
    retry = cfg["executor"].get("capacity_retry", CAPACITY_RETRY_DEFAULTS)
    if not isinstance(retry, dict):
        raise ConfigError("executor.capacity_retry 必须是 JSON object")
    for key in ("immediate", "delayed"):
        value = retry.get(key, CAPACITY_RETRY_DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"executor.capacity_retry.{key} 必须是非负整数")
    delayed_minutes = retry.get("delayed_minutes", CAPACITY_RETRY_DEFAULTS["delayed_minutes"])
    if (isinstance(delayed_minutes, bool) or not isinstance(delayed_minutes, (int, float))
            or not math.isfinite(delayed_minutes) or delayed_minutes <= 0):
        raise ConfigError("executor.capacity_retry.delayed_minutes 必须是大于 0 的有限数字")
    if not isinstance(cfg.get("supervision"), dict):
        raise ConfigError("配置中的 supervision 必须是 JSON object")
    if not isinstance(cfg.get("freeze"), dict):
        raise ConfigError("配置中的 freeze 必须是 JSON object")
    memory = cfg.get("memory", {})
    if not isinstance(memory, dict):
        raise ConfigError("配置中的 memory 必须是 JSON object")
    if memory.get("backend", "mem0") not in {"mem0", "local"}:
        raise ConfigError("memory.backend 只能是 mem0 或 local")
    embedder = memory.get("embedder", {})
    if not isinstance(embedder, dict):
        raise ConfigError("memory.embedder 必须是 JSON object")
    dims = embedder.get("dims", 1024)
    if isinstance(dims, bool) or not isinstance(dims, int) or dims <= 0:
        raise ConfigError("memory.embedder.dims 必须是正整数")
    for key in ("inject_top_k", "core_max_chars"):
        value = memory.get(key, 10 if key == "inject_top_k" else 2200)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError("memory.%s 必须是正整数" % key)
    if "enabled" in cfg["monitor"] and not isinstance(cfg["monitor"]["enabled"], bool):
        raise ConfigError("monitor.enabled 必须是布尔值")
    for key, value in (("monitor.timeout_minutes", cfg["monitor"].get("timeout_minutes", 40)),
                       ("freeze.check_seconds", cfg["freeze"].get("check_seconds", 60)),
                       ("supervision.verify_timeout_minutes", cfg["supervision"].get("verify_timeout_minutes", 30)),
                       ("supervision.rule_check_seconds", cfg["supervision"].get("rule_check_seconds", 60)),
                       ("supervision.first_check_minutes", cfg["supervision"].get("first_check_minutes", 10)),
                       ("supervision.max_check_minutes", cfg["supervision"].get("max_check_minutes", 60)),
                       ("supervision.unhandled_stop_minutes", cfg["supervision"].get("unhandled_stop_minutes", 30))):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"{key} 必须是大于 0 的有限数字")
    for key, value in (("supervision.interval_minutes", cfg["supervision"].get("interval_minutes", 30)),
                       ("supervision.max_unsupervised_minutes", cfg["supervision"].get("max_unsupervised_minutes", 70))):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ConfigError(f"{key} 必须是非负有限数字")
    if isinstance(cfg["supervision"].get("check_summary_max_chars", 300), bool) or not isinstance(
            cfg["supervision"].get("check_summary_max_chars", 300), int) \
            or cfg["supervision"].get("check_summary_max_chars", 300) <= 0:
        raise ConfigError("supervision.check_summary_max_chars 必须是正整数")
    paths = cfg["freeze"].get("paths", [])
    if not isinstance(paths, list):
        raise ConfigError("freeze.paths 必须是路径字符串数组")
    for path in paths:
        _scope_path(path, "freeze.paths")
    if not isinstance(cfg["supervision"].get("test_command", ""), str):
        raise ConfigError("supervision.test_command 必须是字符串")
    alerts = cfg["supervision"].get("alerts", {})
    if not isinstance(alerts, dict):
        raise ConfigError("supervision.alerts 必须是 JSON object")
    for key, value in (("delete_ratio", alerts.get("delete_ratio", 3)),
                       ("stall_minutes", alerts.get("stall_minutes", 20)),
                       ("repeat_fail", alerts.get("repeat_fail", 3))):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ConfigError(f"supervision.alerts.{key} 必须是大于 0 的有限数字")
    return cfg


def run(cmd, cwd=None, env=None, check=True, input_text=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, input=input_text, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {p.stderr.strip()[:400]}")
    return p.stdout


def safe_quota(block=True, notify=False):
    try:
        return codex_quota.get(block=block, notify=notify)
    except Exception:  # noqa: BLE001 — quota is advisory, never fatal
        return None


# ---------------------------------------------------------------- git checkpoints

def is_git(root):
    p = subprocess.run([platform_support.executable("git"), "-C", root, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True, env=clean_git_env(), encoding="utf-8")
    return p.returncode == 0 and p.stdout.strip() == "true"


def git(root, *args, env=None, check=True, input_text=None):
    """git in the user's own repository (statusline helpers and v1 tasks)."""
    return run([platform_support.executable("git"), "-C", root, *args], env=env if env is not None else clean_git_env(),
               check=check, input_text=input_text)


def git_top(root):
    return git(root, "rev-parse", "--show-toplevel").strip()


def snapshot(root, message):
    """Commit object of the whole worktree (tracked + untracked, minus ignored).

    Uses a throwaway index, so the user's index, HEAD and worktree are untouched.
    """
    top = git_top(root)
    head = git(top, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    fd, tmp_index = tempfile.mkstemp(prefix="baton-index-")
    os.close(fd)
    try:
        real_index = git(top, "rev-parse", "--git-path", "index", check=False).strip()
        if real_index and not os.path.isabs(real_index):
            real_index = os.path.join(top, real_index)
        if real_index and os.path.exists(real_index):
            # copy2 keeps the index mtime, so git's racy-clean check still catches same-second edits
            shutil.copy2(real_index, tmp_index)  # keeps the stat cache: fast add
        else:
            os.remove(tmp_index)
        env = clean_git_env({"GIT_INDEX_FILE": tmp_index})
        env.setdefault("GIT_AUTHOR_NAME", "Baton")
        env.setdefault("GIT_AUTHOR_EMAIL", "baton@localhost")
        env.setdefault("GIT_COMMITTER_NAME", "Baton")
        env.setdefault("GIT_COMMITTER_EMAIL", "baton@localhost")
        # assume-unchanged / skip-worktree entries would hide local edits from `add -A`;
        # clear them in the copy (skip-worktree only where the file is actually present)
        assumed, skipped = [], []
        for e in git(top, "ls-files", "-v", "-z", env=env, check=False).split("\0"):
            if len(e) < 3:
                continue
            tag, path = e[0], e[2:]
            if tag in "Ss" and os.path.lexists(os.path.join(top, path)):
                skipped.append(path)
            if tag.islower():
                assumed.append(path)
        if assumed:
            git(top, "update-index", "--no-assume-unchanged", "-z", "--stdin", env=env,
                check=False, input_text="".join(p + "\0" for p in assumed))
        if skipped:
            git(top, "update-index", "--no-skip-worktree", "-z", "--stdin", env=env,
                check=False, input_text="".join(p + "\0" for p in skipped))
        for attempt in range(5):
            p = subprocess.run([platform_support.executable("git"), "-C", top, "add", "-A", "--ignore-errors"], env=env,
                               capture_output=True, text=True, encoding="utf-8")
            if p.returncode in (0, 1):  # 1: some files unreadable, the rest was added
                if p.returncode == 1:
                    print(f"baton: 快照时有文件无法读取，已跳过：{p.stderr.strip()[:300]}", file=sys.stderr)
                break
            time.sleep(0.3 * (attempt + 1))  # usually a file vanished mid-scan; try again
        else:
            raise RuntimeError(f"git add -A 失败：{p.stderr.strip()[:300]}")
        tree = git(top, "write-tree", env=env).strip()
        args = ["commit-tree", tree, "-m", message] + (["-p", head] if head else [])
        return git(top, *args, env=env).strip()
    finally:
        if os.path.exists(tmp_index):
            os.remove(tmp_index)


def _git_author_env(env):
    env.setdefault("GIT_AUTHOR_NAME", "Baton")
    env.setdefault("GIT_AUTHOR_EMAIL", "baton@localhost")
    env.setdefault("GIT_COMMITTER_NAME", "Baton")
    env.setdefault("GIT_COMMITTER_EMAIL", "baton@localhost")
    return env


def clean_git_env(extra=None):
    """os.environ without any variable that redirects git to another repository."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def _locked(path):
    """Open path for an exclusive flock; use as a context manager."""
    if os.name == "nt":
        return platform_support.exclusive_lock(path)
    f = open(path, "a+b")
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    return f


def _probe_ignorecase(directory):
    """True if the file system holding directory ignores letter case; None if it cannot tell."""
    try:
        fd, probe = tempfile.mkstemp(prefix=".baton-case-probe-", dir=directory)
        os.close(fd)
    except OSError:
        return None
    try:
        return os.path.exists(os.path.join(directory, os.path.basename(probe).upper()))
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def validate_external_state(project):
    real_state, real_root = os.path.realpath(project.state_dir), os.path.realpath(project.root)
    if real_state == real_root or real_state.startswith(real_root + os.sep):
        raise RuntimeError(f"项目外状态目录 {project.state_dir} 落在项目目录里，快照会把自己也存进去；"
                           "请把 BATON_HOME 设到项目之外")


def ensure_shadow(project):
    """Create the project's private snapshot repo once.

    The repo is built in a temporary directory under snap.lock and renamed into place, so
    concurrent first snapshots never see a half-made repo.  Its default excludes are written
    only here: later edits by the user are never undone.
    """
    validate_external_state(project)
    gd = project.snap_git_dir
    if os.path.isfile(os.path.join(gd, "HEAD")):
        return gd
    os.makedirs(project.state_dir, exist_ok=True)
    with _locked(os.path.join(project.state_dir, "snap.lock")):
        if os.path.isfile(os.path.join(gd, "HEAD")):
            return gd
        if os.path.isdir(gd):
            os.rmdir(gd)  # a leftover empty dir; a non-empty one without HEAD raises here, on purpose
        tmp = f"{gd}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        env = clean_git_env()
        run([platform_support.executable("git"), "init", "-q", "--bare", tmp], env=env)
        # store working-tree bytes exactly: no end-of-line conversion or filters on the way in or out
        run([platform_support.executable("git"), "--git-dir", tmp, "config", "core.autocrlf", "false"], env=env)
        # git probed case sensitivity where snap.git lives; the project's own volume is what counts
        probe_dir = project.dir if os.path.isdir(project.dir) else project.root
        ignorecase = _probe_ignorecase(probe_dir) if os.path.isdir(probe_dir) else None
        if ignorecase is not None:
            run([platform_support.executable("git"), "--git-dir", tmp, "config", "core.ignorecase", "true" if ignorecase else "false"], env=env)
        os.makedirs(os.path.join(tmp, "info"), exist_ok=True)
        with open(os.path.join(tmp, "info", "attributes"), "w", encoding="utf-8") as f:
            f.write("* -text -filter -ident -working-tree-encoding\n")
        exclude = os.path.join(tmp, "info", "exclude")
        text = read_text(exclude)
        with open(exclude, "a", encoding="utf-8") as f:
            f.write(("\n" if text and not text.endswith("\n") else "")
                    + SHADOW_EXCLUDE_MARK + "\n" + "\n".join(SHADOW_EXCLUDE_DEFAULTS) + "\n")
        os.rename(tmp, gd)
    return gd


class SnapStore:
    """Where a task's checkpoints live.

    "shadow" (v2): Baton's private repo in the project state dir.  Every git call runs with
    GIT_DIR=<snap.git> and GIT_WORK_TREE=<project dir>, so the user's .git, index, HEAD and refs
    are never touched, and ignore files above the project dir (such as a whitelist .gitignore
    of an enclosing repo) do not apply.
    "repo" (v1 tasks): the user's own repository, as Baton v1 used it.
    """

    def __init__(self, project, kind):
        self.project, self.kind = project, kind
        if kind == "shadow":
            self.cwd = self.top = project.root
            self.git_dir = project.snap_git_dir
        else:
            self.cwd = project.root
            self.top = git_top(project.root)
            self.git_dir = None

    def env(self, extra=None):
        env = clean_git_env()
        if self.kind == "shadow":
            env["GIT_DIR"] = self.git_dir
            env["GIT_WORK_TREE"] = self.top
        if extra:
            env.update(extra)
        return env

    def git(self, *args, env=None, check=True, input_text=None, cwd=None):
        return run([platform_support.executable("git"), "-C", cwd or self.cwd, *args], env=self.env(env), check=check, input_text=input_text)

    def has_commit(self, rev):
        if self.kind == "shadow" and not os.path.isdir(self.git_dir):
            return False
        return bool(self.git("rev-parse", "--verify", "-q", f"{rev}^{{commit}}", check=False).strip())

    def user_tracked(self):
        """Paths under the project that the user's own repository tracks (decision D002).

        Read-only: `git ls-files` reads the user's index and writes nothing.  Empty when the
        project is not inside a git repository.
        """
        p = subprocess.run([platform_support.executable("git"), "-C", self.project.root, "ls-files", "-z", "--", "."],
                           env=clean_git_env(), capture_output=True, text=True, encoding="utf-8")
        return {x for x in p.stdout.split("\0") if x} if p.returncode == 0 else set()

    def user_gitlinks(self):
        p = subprocess.run([platform_support.executable("git"), "-C", self.project.root, "ls-files", "-s", "-z", "--", "."],
                           env=clean_git_env(), capture_output=True, text=True, encoding="utf-8")
        links = set()
        for item in p.stdout.split("\0"):
            if item.startswith("160000 ") and "\t" in item:
                links.add(item.split("\t", 1)[1])
        return links

    def snapshot(self, message):
        """Commit object of the project dir; refs are not moved.

        Covers every file that is not excluded, plus the files the user's repository tracks
        even when an ignore rule matches them, as Baton v1 did.  Entries that an exclude added
        after an earlier snapshot now matches leave the shadow index first, so editing the
        exclude file takes effect for files that were already snapshotted.
        """
        if self.kind == "repo":
            return snapshot(self.project.root, message)
        ensure_shadow(self.project)
        top = self.top
        with _locked(os.path.join(self.project.state_dir, "snap.lock")):  # one shadow-index user at a time
            env = _git_author_env(self.env())
            stale_lock = os.path.join(self.git_dir, "index.lock")
            if os.path.exists(stale_lock):
                os.remove(stale_lock)  # every Baton writer holds snap.lock, so this one is left over
            tracked = self.user_tracked()
            now_ignored = [x for x in run([platform_support.executable("git"), "-C", top, "ls-files", "-z", "-ci", "--exclude-standard"],
                                          env=env).split("\0") if x and x not in tracked]
            if now_ignored:
                run([platform_support.executable("git"), "-C", top, "update-index", "--force-remove", "-z", "--stdin"], env=env,
                    input_text="".join(x + "\0" for x in now_ignored))
            for attempt in range(5):
                p = subprocess.run([platform_support.executable("git"), "-C", top, "add", "-A", "--ignore-errors"], env=env,
                                   capture_output=True, text=True, encoding="utf-8")
                if p.returncode in (0, 1):  # 1: some files unreadable, the rest was added
                    if p.returncode == 1:
                        print(f"baton: 快照时有文件无法读取，已跳过：{p.stderr.strip()[:300]}", file=sys.stderr)
                    break
                time.sleep(0.3 * (attempt + 1))  # usually a file vanished mid-scan; try again
            else:
                raise RuntimeError(f"git add -A 失败：{p.stderr.strip()[:300]}")
            if tracked:
                have = set(run([platform_support.executable("git"), "-C", top, "ls-files", "-z"], env=env).split("\0"))
                gitlinks = self.user_gitlinks()
                forced = []
                skipped = []
                for path in sorted(tracked - have):
                    if not os.path.lexists(os.path.join(top, path)):
                        continue
                    if _leading_symlink(top, path):
                        skipped.append(f"{path}（路径经过符号链接）")
                    elif _under(path, gitlinks):
                        skipped.append(f"{path}（gitlink）")
                    else:
                        forced.append(path)
                if skipped:
                    print("baton: 强制加入时跳过：" + "，".join(skipped[:20]), file=sys.stderr)
                if forced:
                    add_cmd = [platform_support.executable("git"), "-C", top, "add", "-f", "--ignore-errors", "--pathspec-from-file=-",
                               "--pathspec-file-nul"]
                    forced_input = "".join(x + "\0" for x in forced)
                    add = subprocess.run(add_cmd, env=dict(env, GIT_LITERAL_PATHSPECS="1"),
                                         input=forced_input, capture_output=True, text=True, encoding="utf-8")
                    if add.returncode != 0:
                        print("baton: 批量强制加入失败，逐个重试：" + add.stderr.strip()[:500], file=sys.stderr)
                        for path in forced:
                            one = subprocess.run(add_cmd, env=dict(env, GIT_LITERAL_PATHSPECS="1"),
                                                 input=path + "\0", capture_output=True, text=True, encoding="utf-8")
                            if one.returncode != 0:
                                print(f"baton: 强制加入失败 {path}：{one.stderr.strip()[:300]}", file=sys.stderr)
            tree = run([platform_support.executable("git"), "-C", top, "write-tree"], env=env).strip()
            return run([platform_support.executable("git"), "-C", top, "commit-tree", tree, "-m", message], env=env).strip()


def store_ref(store, task, label, commit):
    ref, k = f"refs/baton/{task}/{label}", 1
    while store.git("rev-parse", "--verify", "-q", ref, check=False).strip():
        k += 1  # never overwrite an existing checkpoint
        ref = f"refs/baton/{task}/{label}-{k}"
    store.git("update-ref", ref, commit)
    return ref


def checkpoint(store, task, label):
    return store_ref(store, task, label, store.snapshot(f"baton checkpoint {task}/{label} {stamp()}"))


def try_checkpoint(project, task, label, st=None):
    """checkpoint() that reports instead of raising: (ref or None, error or None)."""
    try:
        if not project.task_exists(task):
            return None, f"任务 {task} 不存在（没有简报或状态文件）"
        if project.legacy_state_in_flight(task):
            return None, "任务还有 Baton v1 的一轮在运行，请等这一轮结束"
        store = project.store(task, st)
        if store is None:
            return None, "这个任务建立于 Baton v1，项目又不是 git 仓库，没有回滚点"
        ref = checkpoint(store, task, label)
        if store.kind == "shadow" and (project.read_state(task, {}) or {}).get("snapshot_store") != "shadow":
            # from now on this task's refs live in the shadow repo, whatever the task looked like
            project.update_state(task, lambda s: s.update(snapshot_store="shadow"))
        return ref, None
    except Exception as e:  # noqa: BLE001 — a failed snapshot must not take the caller down
        return None, str(e)


def shortstat(store, a, b):
    """(files, insertions, deletions) between two commits, excluding Baton's own dirs."""
    out = store.git("diff", "--shortstat", a, b, "--", ".", *code_excludes(store), check=False)

    def num(pattern):
        m = re.search(pattern, out)
        return int(m.group(1)) if m else 0
    return num(r"(\d+) files? changed"), num(r"(\d+) insertions?"), num(r"(\d+) deletions?")


# ---------------------------------------------------------------- v2 monitor / dossiers

MONITOR_RECOMMENDATIONS = {
    "report": {"APPROVE", "REVISE", "ROLLBACK"},
    "final": {"ACCEPT", "REVISE"},
    "violation": {"REVERT", "ADOPT", "ASK_USER"},
    "seed": {"APPROVE", "REVISE"},
}
MONITOR_CATEGORIES = {
    "charter_deviation", "precedent_or_message_deviation", "acceptance_gap",
    "breaking_signal", "out_of_scope", "missing_record", "self_report_mismatch",
}
MONITOR_SEVERITIES = {"blocking", "note"}


def monitor_enabled(cfg):
    return cfg.get("mode") != "lean" and (cfg.get("monitor") or {}).get("enabled", True) is not False


def dossier_type_for(status):
    return "final" if status == "done" else "violation" if status == "violation" else "report"


def dossier_id_for(project, task, leg, status):
    kind = dossier_type_for(status)
    if kind == "report":
        artifact = ((leg.get("result") or {}).get("artifact") if isinstance(leg.get("result"), dict) else "") or ""
        match = re.search(r"(?:^|[^A-Za-z])(R\d{3})(?:[^0-9]|$)", os.path.basename(str(artifact)))
        if match:
            candidate = match.group(1)
        else:
            candidate = f"{task}-leg-{leg['n']:03d}"
    elif kind == "violation":
        candidate = f"{task}-leg-{leg['n']:03d}-violation"
    else:
        candidate = f"{task}-final"
        base = os.path.join(project.state_dir, "dossiers")
        if os.path.exists(os.path.join(base, candidate)):
            i = 2
            while os.path.exists(os.path.join(base, f"{candidate}-{i}")):
                i += 1
            candidate = f"{candidate}-{i}"
    return candidate


def curation_dossier_id(project, task):
    base = os.path.join(project.state_dir, "dossiers")
    prefix = task + "-curate-"
    numbers = []
    if os.path.isdir(base):
        for name in os.listdir(base):
            match = re.fullmatch(re.escape(prefix) + r"(\d+)", name)
            if match:
                numbers.append(int(match.group(1)))
    return prefix + "%02d" % (max(numbers or [0]) + 1)


def dossier_path(project, dossier_id):
    if not isinstance(dossier_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", dossier_id):
        raise ConfigError("卷宗号必须是字母、数字、下划线或短横线，不能包含路径")
    return os.path.join(project.state_dir, "dossiers", dossier_id)


def dossier_out_dir(project, dossier_id):
    return os.path.join(project.state_dir, "dossiers", dossier_id + ".out")


def _dossier_write(path, text, binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if binary:
        with open(path, "wb") as stream:
            stream.write(text)
    else:
        with open(path, "w", encoding="utf-8") as stream:
            stream.write(text)


_WIN_MAX_PATH = 260  # MAX_PATH incl. the terminating NUL
_WIN_MAX_DIR = 248   # CreateDirectory leaves room for an 8.3 file name


def _dossier_code_snapshot(store, ref, target):
    omissions = {"files": 0, "bytes": 0, "reason": "项目 git ignore 规则忽略的未跟踪文件"}
    os.makedirs(target, exist_ok=True)
    if not store or not ref:
        return omissions
    proc = subprocess.Popen([platform_support.executable("git"), "-C", store.cwd, "archive", ref, ".", *code_excludes(store)],
                            env=store.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    data, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(err.decode("utf-8", "replace")[:500])
    archive_path = tempfile.mktemp(prefix="baton-dossier-", suffix=".tar")
    try:
        with open(archive_path, "wb") as stream:
            stream.write(data)
        with tarfile.open(archive_path, "r") as archive:
            members = archive.getmembers()
            ignored = set()
            if is_git(store.project.root):
                # The user's index exempts tracked files; never use the shadow index here.
                paths = [member.name for member in members if not member.isdir()]
                if paths:
                    p = subprocess.run([platform_support.executable("git"), "-C", store.project.root,
                                        "check-ignore", "-z", "--stdin"], env=clean_git_env(),
                                       input="".join(path + "\0" for path in paths),
                                       capture_output=True, text=True, encoding="utf-8")
                    if p.returncode not in (0, 1):
                        raise RuntimeError("卷宗 git ignore 检查失败：" + p.stderr.strip()[:500])
                    ignored = {path for path in p.stdout.split("\0") if path}
            for member in members:
                if member.name in ignored:
                    omissions["files"] += 1
                    omissions["bytes"] += member.size
                    continue
                name = member.name.replace("\\", "/")
                if name.startswith(".baton/") or name == ".baton":
                    continue
                destination = os.path.realpath(os.path.join(target, name))
                if destination != os.path.realpath(target) and not destination.startswith(os.path.realpath(target) + os.sep):
                    continue
                if os.name == "nt" and len(destination) >= (_WIN_MAX_DIR if member.isdir() else _WIN_MAX_PATH):
                    # Without LongPathsEnabled the copy cannot be created (e.g. deep pytest basetemp dirs).
                    if not member.isdir():
                        long_paths = omissions.setdefault("long_paths", {"files": 0, "bytes": 0, "examples": []})
                        long_paths["files"] += 1
                        long_paths["bytes"] += member.size
                        if len(long_paths["examples"]) < 5:
                            long_paths["examples"].append(name)
                    continue
                if member.isdir():
                    # tarfile creates directories with mode 0o700, which on Windows (Python 3.12.4+) becomes an
                    # owner-only ACL that the monitor's sandbox cannot read; makedirs keeps the inherited ACL.
                    os.makedirs(destination, exist_ok=True)
                    continue
                archive.extract(member, target)
    finally:
        try:
            os.remove(archive_path)
        except OSError:
            pass
    return omissions


def _dossier_files(root):
    out = {}
    for directory, _, names in os.walk(root):
        for name in names:
            path = os.path.join(directory, name)
            rel = os.path.relpath(path, root).replace(os.sep, "/")
            if rel == "manifest.json":
                continue
            try:
                with open(path, "rb") as stream:
                    out[rel] = hashlib.sha256(stream.read()).hexdigest()
            except OSError:
                continue
    return out


def _dossier_total(files):
    return hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dossier_manifest(project, dossier_id):
    path = os.path.join(dossier_path(project, dossier_id), "manifest.json")
    return read_json(path, {}) or {}


def verify_dossier(project, dossier_id, require_code=False):
    root = dossier_path(project, dossier_id)
    manifest = dossier_manifest(project, dossier_id)
    files = _dossier_files(root) if os.path.isdir(root) else {}
    expected = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(expected, dict):
        return False, "缺少卷宗 manifest"
    if require_code and manifest.get("code_pruned"):
        return False, "卷宗代码已清理，不能重跑 monitor"
    retained = {k: v for k, v in expected.items() if not (manifest.get("code_pruned") and k.startswith("code/"))}
    if files != retained:
        missing = sorted(set(retained) - set(files))
        changed = sorted(k for k in set(retained) & set(files) if retained[k] != files[k])
        extra = sorted(set(files) - set(retained))
        detail = ", ".join(((["缺少 " + ",".join(missing)] if missing else []) +
                            (["改动 " + ",".join(changed)] if changed else []) +
                            (["新增 " + ",".join(extra)] if extra else [])))
        return False, "卷宗文件校验失败" + ("：" + detail if detail else "")
    if manifest.get("total_sha256") != _dossier_total(expected):
        return False, "卷宗总哈希不匹配"
    return True, ""


def _seal_dossier(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            try:
                os.chmod(os.path.join(directory, name), 0o444)
            except OSError:
                pass
        try:
            os.chmod(directory, 0o555)
        except OSError:
            pass


def _unseal_dossier(root):
    for directory, _, names in os.walk(root, topdown=False):
        for name in names:
            try:
                os.chmod(os.path.join(directory, name), 0o644)
            except OSError:
                pass
        try:
            os.chmod(directory, 0o755)
        except OSError:
            pass


def _rmtree_retry(path, attempts=5, delay=0.5):
    # On Windows a file still held open (scanner, indexer, a just-exited monitor) is only marked for deletion,
    # so removing its directory can fail with "directory not empty" (WinError 145) until the handle closes.
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _prune_dossier_code(project, dossier_id, reason, dry_run=False):
    root = dossier_path(project, dossier_id)
    code = os.path.join(root, "code")
    if not os.path.isdir(code):
        return None
    if not os.path.realpath(code).startswith(os.path.realpath(root) + os.sep):
        raise RuntimeError("卷宗 code 路径不在卷宗内：" + code)
    sizes = [os.lstat(os.path.join(directory, name)).st_size
             for directory, _, names in os.walk(code) for name in names]
    removed = {"files": len(sizes), "bytes": sum(sizes), "reason": reason, "at": now()}
    if not dry_run:
        incomplete = not os.path.exists(os.path.join(root, "manifest.json"))
        manifest = {} if incomplete else dossier_manifest(project, dossier_id)
        if not incomplete and not manifest.get("files"):
            raise RuntimeError("卷宗缺少文件清单，不能清理：" + dossier_id)
        os.chmod(root, 0o755)
        try:
            _unseal_dossier(code)
            _rmtree_retry(code)
            # An interrupted build has no manifest and nothing bound to it in the ledger; otherwise
            # keep the original inventory/hash so existing ledger bindings still resolve.
            if not incomplete:
                manifest["code_pruned"] = removed
                os.chmod(os.path.join(root, "manifest.json"), 0o644)
                write_json(os.path.join(root, "manifest.json"), manifest)
        finally:
            _seal_dossier(root)
    return removed


def task_dossiers(project, task, st):
    """Find both current manifests and pre-retention dossiers recorded in task state/ledger."""
    known = {record["dossier"] for record in ledger_records(project)
             if record.get("task") == task and record.get("dossier")}
    for leg in st.get("legs", []):
        for key in ("dossier", "monitor", "monitor_check", "last_check"):
            value = leg.get(key) or {}
            ident = value.get("id") or value.get("dossier")
            if ident:
                known.add(ident)
        if leg.get("check_dossier"):
            known.add(leg["check_dossier"])
    base = os.path.join(project.state_dir, "dossiers")
    if not os.path.isdir(base):
        return []
    found = []
    pattern = re.escape(task) + r"-(?:final(?:-\d+)?|leg-\d+(?:-(?:violation|c\d+))?|curate-\d+|seed)"
    for ident in sorted(os.listdir(base)):
        if ident.endswith(".out") or not os.path.isdir(os.path.join(base, ident)):
            continue
        manifest = dossier_manifest(project, ident)
        owner = manifest.get("task")
        if owner == task or (not owner and (ident in known or re.fullmatch(pattern, ident))):
            found.append(ident)
    return found


def prune_closed_task(project, task, dry_run=False):
    st = project.load_state(task)
    if st.get("status") != "closed":
        return []
    removed = []
    for ident in task_dossiers(project, task, st):
        result = _prune_dossier_code(project, ident, "任务已 closed", dry_run=dry_run)
        if result is not None:
            removed.append(result)
            print(f"{'将清理' if dry_run else '已清理'} {ident}/code/：{result['files']} 个文件，{result['bytes']} 字节")
    return removed


def cmd_gc(project, args):
    project.require_init()
    removed = []
    for task in project.tasks():
        removed.extend(prune_closed_task(project, task, dry_run=args.dry_run))
    print(f"{'将清理' if args.dry_run else '已清理'} {len(removed)} 份卷宗代码，"
          f"{sum(item['files'] for item in removed)} 个文件，{sum(item['bytes'] for item in removed)} 字节")


def _charter_text(project, task, st):
    brief = read_text(os.path.join(project.task_dir(task), "brief.md"), "")
    chunks = ["# 冻结章程\n\n", brief]
    for amendment in st.get("amendments") or []:
        path = amendment.get("path") if isinstance(amendment, dict) else None
        if path:
            chunks.append("\n\n## 修正案 " + str(amendment.get("id", "")) + "\n\n" + read_text(path))
    return "".join(chunks)


def _precedents_text(project, task):
    chunks = ["# 判例、审阅与指挥消息\n"]
    if not verify_ledger(project):
        raise ConfigError("账本校验失败")
    for record in ledger_records(project):
        if record.get("task") != task:
            continue
        if record.get("type") == "decision":
            chunks.append(f"\n## 判例 {record.get('decision_id')} · seq {record['seq']}\n\n"
                          + record.get("decision", "") + "\n")
        elif record.get("type") == "verdict":
            chunks.append(f"\n## 裁决 {record.get('dossier')} · seq {record['seq']}\n\n"
                          f"结论：{record.get('conclusion')}\n")
            for fid, response in (record.get("findings") or {}).items():
                chunks.append(f"- {fid}：{response['decision']}；{response['reason']}\n")
        elif record.get("type") == "message":
            chunks.append(f"\n## 指挥消息 {record.get('number')} · {record.get('kind')} · seq {record['seq']}\n\n"
                          + record.get("message", "") + "\n")
    return "".join(chunks)


def _scope_findings(project, task, st, baseline, end_ref):
    store = project.store(task, st)
    if not store or not baseline or not end_ref:
        return []
    pairs = diff_tree_entries(store, baseline, end_ref)
    scope = st.get("scope") or {}
    import fnmatch
    violations = []
    for _, status, _, path in pairs:
        allowed = any(fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("/") + "/")
                      for pat in scope.get("allow", []))
        denied = any(fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("/") + "/")
                     for pat in scope.get("deny", []))
        if not allowed or denied:
            violations.append(f"{status} {path}（{'命中 deny' if denied else '不在 allow'}）")
    return violations


def _evidence_text(project, task, st, leg, dossier_kind, baseline):
    verify = leg.get("verify") or {}
    lines = []
    if verify.get("skipped") and "没有 baton 亲跑的验证" in (verify.get("skip_reason") or ""):
        lines.append("本任务没有 baton 亲跑的验证")
    probe = read_json(os.path.join(project.state_dir, "monitor-probe.json"), {}) or {}
    if probe.get("codex_global_instructions"):
        lines.append("monitor 带着用户的 Codex 全局指令（~/.codex/AGENTS.md）")
    elif probe.get("codex_global_instructions") is None:
        lines.append("monitor 隔离情况未经探针确认")
    lines += ["", "## Diff", f"本轮改动：{leg.get('diff') or '无'}"]
    store = project.store(task, st)
    if store and baseline and leg.get("end_ref"):
        lines.append(store.git("diff", "--stat=120", baseline, leg["end_ref"], "--", ".", *code_excludes(store), check=False).rstrip() or "（无）")
        lines.append("\n按文件摘要：")
        for status, path in [(s, p) for _, _, s, p in diff_tree_entries(store, baseline, leg["end_ref"])]:
            lines.append(f"- {status} {path}")
    lines += ["", "## 验证", _verification_summary(verify), json.dumps(verify, ensure_ascii=False, indent=2)]
    lines += ["", "## 越界检查", "\n".join(_scope_findings(project, task, st, baseline, leg.get("end_ref"))) or "无"]
    check = check_freeze(project, task, st)
    lines += ["", "## 冻结检查", freeze_summary(check) or "无"]
    if check.get("monitor"):
        lines.append("监看级改动：" + ", ".join(x.get("path", "?") for x in check["monitor"]))
    lines += ["", "## 执行者命令"]
    calls = leg_terminal_calls(project, task, leg)
    lines += [f"- [{call.get('exit_code')}] {call.get('command')} @ {call.get('time')}" for call in calls] or ["无"]
    lines += ["", "## 本轮的外部动作"]
    external = leg.get("external_actions") or []
    lines += [f"- {json.dumps(action, ensure_ascii=False, sort_keys=True)}" for action in external] or ["无"]
    lines += ["", "## 轮中检查", "无"]
    lines += ["", "## 汇报性质", "report.html 是执行者的当事人陈述"]
    return "\n".join(lines) + "\n"


def _dossier_memory_candidates(project, task, st, leg, report_text=""):
    """Collect report, decision and (later) monitor candidates for one dossier."""
    from baton_memory.candidates import CandidateError, nearest_candidates, number_candidates, parse_memory_candidates
    try:
        candidates = parse_memory_candidates(report_text or "")
    except CandidateError as exc:
        raise ConfigError("执行者汇报里的记忆候选无效：%s" % exc) from exc
    # A conductor decision is a first-class precedent candidate in the next
    # dossier.  It remains in the ruling ledger as the authoritative source.
    for record in ledger_records(project):
        if record.get("type") != "decision" or record.get("task") != task:
            continue
        decision_id = str(record.get("decision_id") or "")
        text = str(record.get("decision") or "").strip()
        if not decision_id or not text:
            continue
        candidates.append({"kind": "precedent", "text": text, "source": "conductor",
                           "anchors": [decision_id], "decision_id": decision_id})
    candidates = number_candidates(candidates)
    try:
        from baton_memory.ledger import MemoryLedger
        ledger = MemoryLedger(project)
        semantic = None
        if memory_installed(config_for_state(project, st)):
            try:
                from baton_memory.mem0_adapter import Mem0Index
                semantic = Mem0Index(ledger)
            except Exception:
                semantic = None
        for candidate in candidates:
            candidate["nearest"] = nearest_candidates(ledger, candidate, top_k=3, semantic=semantic)
    except Exception:
        # A dossier must still be buildable before the optional venv is
        # installed; an empty nearest list is explicit evidence of that state.
        for candidate in candidates:
            candidate["nearest"] = []
    return candidates


def _memory_dossier_text(project, task, st, leg, candidates=None):
    """Render auditable memory inputs and the executor's self-report."""
    lines = ["# 本轮记忆", "", "## 推送条目"]
    pushed = list(leg.get("memory_pushed") or []) if isinstance(leg, dict) else []
    if pushed:
        lines.extend("- " + item for item in pushed)
    else:
        lines.append("- 本轮没有推送条目")
    if leg.get("memory_withdrawals"):
        lines.extend(["", "## 撤回"])
        lines.extend("- " + str(item) for item in leg["memory_withdrawals"])
    calls = project.leg_path(task, leg.get("n", 0), "memory-calls.jsonl") if leg.get("n") else ""
    returned = []
    if calls and os.path.isfile(calls):
        for raw in read_text(calls).splitlines():
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("ledger_id"):
                returned.append(str(value["ledger_id"]))
    lines.extend(["", "## memory_search 实际返回"])
    lines.append("- " + ", ".join(dict.fromkeys(returned)) if returned else "- 本轮没有 memory-calls 记录")
    used = ((leg.get("result") or {}).get("memory_used") if isinstance(leg.get("result"), dict) else None) or []
    lines.extend(["", "## 执行者自报 memory_used"])
    lines.append("- " + ", ".join(map(str, used)) if used else "- []")
    lines.extend(["", "## 当前常驻块", leg.get("memory_core") or "- 本轮没有常驻块"])
    try:
        from baton_memory.ledger import MemoryLedger
        suspended = [item for item in MemoryLedger(project).latest().values() if item.get("status") == "suspended"]
    except Exception:
        suspended = []
    lines.extend(["", "## 回滚后挂起记忆"])
    if suspended:
        lines.extend("- [%s] %s（来源快照：%s）" % (item.get("id"), item.get("text", ""), item.get("end_snapshot", ""))
                     for item in suspended)
    else:
        lines.append("- 无")
    lines.extend(["", "## 记忆候选"])
    if not candidates:
        lines.append("本阶段无")
    else:
        for candidate in candidates:
            lines.extend(["### %s · %s" % (candidate["id"], candidate["kind"]),
                          "正文：" + candidate["text"],
                          "出处：" + candidate.get("source", ""),
                          "锚点：" + ", ".join(candidate.get("anchors") or [])])
            nearest = candidate.get("nearest") or []
            lines.append("最相近已有条目（两路检索，最多 3 条）：")
            if nearest:
                lines.extend("- [%s] %s" % (item.get("ledger_id", item.get("id", "?")), item.get("text", ""))
                             for item in nearest)
            else:
                lines.append("- 无")
    return "\n".join(lines) + "\n"


def build_dossier(project, task, st, leg, status):
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg) or status not in ("report", "done", "violation"):
        return None
    precedents = _precedents_text(project, task)
    dossier_id = dossier_id_for(project, task, leg, status)
    root = dossier_path(project, dossier_id)
    if os.path.exists(root):
        _unseal_dossier(root)
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    kind = dossier_type_for(status)
    baseline = report_baseline(st, leg) if kind == "report" else st.get("base_ref")
    store = project.store(task, st)
    prompt = fill(read_text(os.path.join(TEMPLATES, "monitor-prompt.md")),
                  {"TASK": task, "DOSSIER": dossier_id, "DOSSIER_TYPE": kind})
    _dossier_write(os.path.join(root, "MONITOR_PROMPT.md"), prompt)
    _dossier_write(os.path.join(root, "charter.md"), _charter_text(project, task, st))
    _dossier_write(os.path.join(root, "evidence.md"), _evidence_text(project, task, st, leg, kind, baseline))
    diff_text = ""
    if store and baseline and leg.get("end_ref"):
        diff_text = store.git("diff", "--binary", baseline, leg["end_ref"], "--", ".", *code_excludes(store), check=False)
    _dossier_write(os.path.join(root, "diff.patch"), diff_text)
    omissions = _dossier_code_snapshot(store, leg.get("end_ref"), os.path.join(root, "code"))
    artifact = ((leg.get("result") or {}).get("artifact") if isinstance(leg.get("result"), dict) else "") or ""
    report_text = ""
    if artifact and os.path.isfile(artifact):
        shutil.copy2(artifact, os.path.join(root, "report.html"))
        report_text = read_text(artifact)
    else:
        _dossier_write(os.path.join(root, "report.html"), "执行者没有提供汇报原件；本文件是占位说明。\n")
    candidates = _dossier_memory_candidates(project, task, st, leg, report_text)
    _dossier_write(os.path.join(root, "memory.md"), _memory_dossier_text(project, task, st, leg, candidates))
    _dossier_write(os.path.join(root, "precedents.md"), precedents)
    files = _dossier_files(root)
    write_json(os.path.join(root, "manifest.json"), {"dossier": dossier_id, "task": task, "leg": leg.get("n"),
        "type": kind, "code_omissions": omissions, "files": files,
        "total_sha256": _dossier_total(files), "created_at": now()})
    _seal_dossier(root)
    return dossier_id


def _curation_rows(project):
    from baton_memory.ledger import MemoryLedger
    ledger = MemoryLedger(project)
    latest = ledger.latest()
    return [dict(value) for value in latest.values()
            if value.get("status", "active") in {"active", "superseded"}]


def _curation_text(project, task):
    from baton_memory.search import render_pinned_block
    from baton_memory.ledger import MemoryLedger
    ledger = MemoryLedger(project)
    rows = sorted(_curation_rows(project), key=lambda x: str(x.get("id", "")))
    pinned, truncated, _ = render_pinned_block(ledger, budget=10 ** 9)
    lines = ["# 整理对象", "", "## 全部 active 和 superseded 条目"]
    for row in rows:
        lines.extend(["### %s · %s · %s" % (row.get("id"), row.get("kind"), row.get("status")),
                      "正文：" + str(row.get("text", "")),
                      "来源：" + str(row.get("source", "")),
                      "字符数：%d" % len(str(row.get("text", "")))])
    lines.extend(["", "## 常驻块全文", pinned, "字符数：%d" % len(pinned)])
    records = ledger_records(project)
    last = max((int(x.get("seq", 0)) for x in records if x.get("type") == "curation" and x.get("task") == task), default=0)
    lines.extend(["", "## 上次整理以来的裁决要点"])
    recent = [x for x in records if int(x.get("seq", 0)) > last and x.get("task") == task
              and x.get("type") in {"verdict", "decision"}]
    if recent:
        for item in recent:
            lines.append("- %s：%s" % (item.get("type"), item.get("conclusion") or item.get("decision", "")))
    else:
        lines.append("- 无")
    if truncated:
        lines.append("- 常驻块按整理卷宗不截断；推送预算由下一轮事件检查")
    return "\n".join(lines) + "\n"


def build_curation_dossier(project, task):
    st = project.load_state(task) if os.path.isfile(project.state_path(task)) else {"task": task, "legs": []}
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg):
        return None
    dossier_id = curation_dossier_id(project, task)
    root = dossier_path(project, dossier_id)
    os.makedirs(root, exist_ok=True)
    prompt = fill(read_text(os.path.join(TEMPLATES, "monitor-curation-prompt.md")),
                  {"TASK": task, "DOSSIER": dossier_id})
    _dossier_write(os.path.join(root, "MONITOR_PROMPT.md"), prompt)
    _dossier_write(os.path.join(root, "charter.md"), _charter_text(project, task, st))
    _dossier_write(os.path.join(root, "curation.md"), _curation_text(project, task))
    _dossier_write(os.path.join(root, "memory.md"), _curation_text(project, task))
    leg = current_leg(st) or {"n": 0, "events": "", "started_at": now()}
    _dossier_write(os.path.join(root, "evidence.md"), _evidence_text(project, task, st, leg, "curation", st.get("base_ref")))
    store = project.store(task, st)
    omissions = {"files": 0, "bytes": 0, "reason": "项目 git ignore 规则忽略的未跟踪文件"}
    if store and (leg.get("end_ref") or st.get("base_ref")):
        omissions = _dossier_code_snapshot(store, leg.get("end_ref") or st.get("base_ref"), os.path.join(root, "code"))
    _dossier_write(os.path.join(root, "diff.patch"), "")
    files = _dossier_files(root)
    write_json(os.path.join(root, "manifest.json"), {"dossier": dossier_id, "task": task, "leg": leg.get("n"),
        "type": "curation", "code_omissions": omissions,
        "files": files, "total_sha256": _dossier_total(files), "created_at": now()})
    _seal_dossier(root)
    return dossier_id


def _validate_curation(value):
    errors = []
    if not isinstance(value, dict):
        return ["整理输出不是 JSON object"]
    if set(value) != {"summary", "operations"}:
        errors.append("整理输出只能包含 summary 和 operations")
    if not isinstance(value.get("summary"), str) or len(value.get("summary", "")) > 600:
        errors.append("summary 必须是不超过 600 字的字符串")
    ops = value.get("operations")
    if not isinstance(ops, list):
        return errors + ["operations 必须是数组"]
    fields = {"id", "op", "targets", "kind", "text", "source", "anchors", "reason"}
    for index, item in enumerate(ops, 1):
        if not isinstance(item, dict):
            errors.append("operation %d 必须是 object" % index)
            continue
        if set(item) != fields:
            errors.append("operation %d 字段不匹配" % index)
        if item.get("id") != "K%d" % index:
            errors.append("operation 编号应为 K%d" % index)
        if item.get("op") not in {"merge", "supersede", "pin", "unpin", "retire"}:
            errors.append("operation op 无效")
        if (not isinstance(item.get("targets"), list) or not item["targets"]
                or any(not isinstance(x, str) or not re.fullmatch(r"M[0-9]*[1-9][0-9]*", x) for x in item["targets"])):
            errors.append("operation targets 必须是 M 编号数组")
        if item.get("kind") not in {"command", "convention", "pitfall", "precedent", "map", "event"}:
            errors.append("operation kind 无效")
        for key in ("text", "source", "reason"):
            if not isinstance(item.get(key), str):
                errors.append("operation %s 必须是字符串" % key)
        anchors = item.get("anchors")
        if not isinstance(anchors, list) or not 1 <= len(anchors) <= 3 or any(not isinstance(x, str) for x in anchors):
            errors.append("operation anchors 必须有 1 到 3 个字符串")
    return errors


def _validate_monitor_brief(value, dossier_kind, candidate_ids=None):
    errors = []
    if not isinstance(value, dict):
        return ["输出不是 JSON object"]
    required = {"summary", "recommendation", "findings", "memory_advice"}
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        errors.append("缺少字段：" + ", ".join(missing))
    if extra:
        errors.append("多余字段：" + ", ".join(extra))
    summary = value.get("summary")
    if not isinstance(summary, str):
        errors.append("summary 必须是字符串")
    elif len(summary) > 600:
        errors.append("summary 超过 600 字")
    recommendation = value.get("recommendation")
    if not isinstance(recommendation, str) or recommendation not in MONITOR_RECOMMENDATIONS.get(dossier_kind, set()):
        errors.append(f"recommendation {recommendation!r} 不适用于 {dossier_kind}")
    findings = value.get("findings")
    if not isinstance(findings, list):
        errors.append("findings 必须是数组")
        findings = []
    expected_id = 1
    for item in findings:
        if not isinstance(item, dict):
            errors.append("finding 必须是 object")
            continue
        fields = {"id", "category", "severity", "description", "evidence", "rule_source"}
        miss = sorted(fields - set(item))
        extra_item = sorted(set(item) - fields)
        if miss:
            errors.append("finding 缺少字段：" + ", ".join(miss))
        if extra_item:
            errors.append("finding 有多余字段：" + ", ".join(extra_item))
        expected = f"F{expected_id}"
        if item.get("id") != expected:
            errors.append(f"finding 编号应为 {expected}")
        expected_id += 1
        if not isinstance(item.get("category"), str) or item.get("category") not in MONITOR_CATEGORIES:
            errors.append(f"finding category 无效：{item.get('category')!r}")
        if not isinstance(item.get("severity"), str) or item.get("severity") not in MONITOR_SEVERITIES:
            errors.append(f"finding severity 无效：{item.get('severity')!r}")
        for key in ("description", "rule_source"):
            if not isinstance(item.get(key), str):
                errors.append(f"finding {key} 必须是字符串")
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            errors.append("finding evidence 必须是数组")
        else:
            for ev in evidence:
                if not isinstance(ev, dict) or set(ev) != {"location", "excerpt"} \
                        or not all(isinstance(ev.get(k), str) for k in ("location", "excerpt")):
                    errors.append("finding evidence 必须只含字符串 location 和 excerpt")
    advice = value.get("memory_advice")
    if not isinstance(advice, list):
        errors.append("memory_advice 必须是对象数组")
        advice = []
    advice_ids = set()
    memory_fields = {"id", "action", "kind", "text", "source", "anchors", "reason"}
    allowed_actions = {"approve", "rewrite", "reject", "new"}
    allowed_kinds = {"command", "convention", "pitfall", "precedent", "map", "event"}
    for item in advice:
        if not isinstance(item, dict):
            errors.append("memory_advice 每项必须是 object")
            continue
        missing = sorted(memory_fields - set(item))
        extra = sorted(set(item) - memory_fields)
        if missing:
            errors.append("memory_advice 缺少字段：" + ", ".join(missing))
        if extra:
            errors.append("memory_advice 有多余字段：" + ", ".join(extra))
        ident = item.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"C[1-9][0-9]*|M[0-9]*[1-9][0-9]*|new", ident):
            errors.append("memory_advice.id 必须是 C/M 编号或 new")
        elif candidate_ids is not None and ident.startswith("C") and ident not in set(candidate_ids):
            errors.append("memory_advice.id 不在本卷宗候选中：" + ident)
        elif ident in advice_ids:
            errors.append("memory_advice.id 重复：" + ident)
        else:
            advice_ids.add(ident)
        if item.get("action") not in allowed_actions | {"restore", "retire"}:
            errors.append("memory_advice.action 无效：%r" % (item.get("action"),))
        if item.get("kind") not in allowed_kinds:
            errors.append("memory_advice.kind 无效：%r" % (item.get("kind"),))
        for key in ("text", "source", "reason"):
            if not isinstance(item.get(key), str) or not item.get(key).strip():
                errors.append("memory_advice.%s 必须是非空字符串" % key)
        anchors = item.get("anchors")
        if (not isinstance(anchors, list) or not 1 <= len(anchors) <= 3
                or any(not isinstance(x, str) or not x.strip() for x in anchors)):
            errors.append("memory_advice.anchors 必须有 1 到 3 个字符串")
        if item.get("id") == "new" and item.get("action") != "new":
            errors.append("id=new 的 memory_advice.action 必须是 new")
        if item.get("id") != "new" and item.get("action") == "new":
            errors.append("已有候选不能使用 action=new")
        if isinstance(item.get("id"), str) and item["id"].startswith("M") and item.get("action") not in {"restore", "retire", "approve", "reject"}:
            errors.append("挂起记忆只能 restore 或 retire")
        if isinstance(item.get("id"), str) and item["id"].startswith("C") and item.get("action") in {"restore", "retire"}:
            errors.append("普通候选不能 restore 或 retire")
    return errors


def _validate_monitor_check(value, max_chars=300):
    """Validate the smaller JSON result used by an asynchronous leg check."""
    errors = []
    if not isinstance(value, dict):
        return ["输出不是 JSON object"]
    required = {"conclusion", "summary", "findings"}
    missing = sorted(required - set(value))
    extra = sorted(set(value) - required)
    if missing:
        errors.append("缺少字段：" + ", ".join(missing))
    if extra:
        errors.append("多余字段：" + ", ".join(extra))
    if value.get("conclusion") not in {"on_track", "drifting", "breaking"}:
        errors.append("conclusion 必须是 on_track、drifting 或 breaking")
    summary = value.get("summary")
    if not isinstance(summary, str):
        errors.append("summary 必须是字符串")
    elif len(summary) > int(max_chars):
        errors.append(f"summary 超过 {max_chars} 字")
    findings = value.get("findings")
    if not isinstance(findings, list):
        errors.append("findings 必须是数组")
        findings = []
    expected_id = 1
    for item in findings:
        if not isinstance(item, dict):
            errors.append("finding 必须是 object")
            continue
        fields = {"id", "category", "severity", "description", "evidence", "rule_source"}
        if set(item) != fields:
            errors.append("finding 字段不符合监督简报结构")
        if item.get("id") != f"F{expected_id}":
            errors.append(f"finding 编号应为 F{expected_id}")
        expected_id += 1
        if item.get("category") not in MONITOR_CATEGORIES:
            errors.append(f"finding category 无效：{item.get('category')!r}")
        if item.get("severity") not in MONITOR_SEVERITIES:
            errors.append(f"finding severity 无效：{item.get('severity')!r}")
        if not isinstance(item.get("description"), str) or not isinstance(item.get("rule_source"), str):
            errors.append("finding description 和 rule_source 必须是字符串")
        if not isinstance(item.get("evidence"), list):
            errors.append("finding evidence 必须是数组")
        else:
            for evidence in item["evidence"]:
                if not isinstance(evidence, dict) or set(evidence) != {"location", "excerpt"} \
                        or not all(isinstance(evidence.get(key), str) for key in ("location", "excerpt")):
                    errors.append("finding evidence 必须只含字符串 location 和 excerpt")
    return errors


def _monitor_codex_command(project, dossier_id, cfg, output_path, effort=None, schema_path=None):
    model = (cfg.get("monitor") or {}).get("model")
    command = [platform_support.executable("codex"), "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral",
               "--skip-git-repo-check", "-s", "read-only", "-C", dossier_path(project, dossier_id),
               "-m", model]
    command += platform_support.codex_readonly_config()
    if effort is not None:
        command += ["-c", f'model_reasoning_effort="{effort}"']
    command += ["--output-schema", schema_path or os.path.join(TEMPLATES, "monitor-brief.schema.json"), "-o", output_path, "-"]
    return command


def _monitor_claude_command(project, dossier_id, cfg, effort=None, stream=False, schema_path=None):
    model = (cfg.get("monitor") or {}).get("model")
    command = ["claude", "-p", "--safe-mode", "--strict-mcp-config", "--setting-sources", "",
               "--permission-mode", "dontAsk", "--tools", "Read,Grep,Glob", "--add-dir",
               os.path.join(dossier_path(project, dossier_id), "code"), "--model", model,
               "--no-session-persistence", "--output-format", "stream-json" if stream else "json"]
    schema = json.loads(read_text(schema_path or os.path.join(TEMPLATES, "monitor-brief.schema.json")))
    schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    command += ["--json-schema", schema_text]
    if stream:
        command += ["--verbose"]
    return command


def _monitor_effort(cfg, dossier_kind):
    monitor_cfg = cfg.get("monitor") or {}
    if dossier_kind == "final" and monitor_cfg.get("final_reasoning_effort") is not None:
        return monitor_cfg.get("final_reasoning_effort")
    return monitor_cfg.get("reasoning_effort")


def _monitor_result_from_files(project, dossier_id, model, output_path, stdout):
    if model in CONDUCTOR_MODELS:
        try:
            value = json.loads(stdout or "")
        except (TypeError, ValueError):
            return None, "Claude 输出不是 JSON"
        if isinstance(value, dict) and isinstance(value.get("structured_output"), dict):
            if value.get("is_error") or value.get("subtype") != "success":
                return None, "Claude 返回错误"
            return value["structured_output"], ""
        return None, "Claude 输出缺少 structured_output"
    value = read_json(output_path)
    return (value, "") if value is not None else (None, "Codex 没有写出 JSON 简报")


def _monitor_read_error(value, diagnostics=""):
    """An unreadable dossier is a monitor failure, never evidence of executor drift."""
    for line in diagnostics.splitlines():
        if "blocked by policy" not in line.lower():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return "monitor 基础设施错误：卷宗读取被策略阻止（blocked by policy）"
        if isinstance(event, dict):
            item = event.get("item") or {}
            if event.get("type") in ("error", "turn.failed") or event.get("error") or (
                    isinstance(item, dict) and item.get("type") == "command_execution" and
                    (item.get("status") == "failed" or item.get("exit_code") not in (None, 0))):
                return "monitor 基础设施错误：卷宗读取被策略阻止（blocked by policy）"
    texts = [value.get("summary", "")] if isinstance(value, dict) else []
    findings = value.get("findings") if isinstance(value, dict) else None
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                texts.append(json.dumps(finding, ensure_ascii=False))
    for text in texts:
        if not isinstance(text, str):
            continue
        lower = text.lower()
        dossier = any(word in lower for word in ("dossier", "卷宗", "charter.md", "evidence.md",
                      "manifest.json", "monitor_prompt.md", "precedents.md", "memory.md"))
        unreadable = re.search(r"cannot read|can't read|could not read|unable to (?:read|access)|failed to read|"
                               r"blocked by policy|permission denied|access denied|无法(?:读取|访问)|不能读取|"
                               r"未能读取|读取失败|拒绝访问|策略阻止", lower)
        if dossier and unreadable:
            return "monitor 基础设施错误：无法读取卷宗；" + text[:500]
    return ""


def run_monitor_once(project, task, dossier_id, dossier_kind, cfg, prompt, leg_n=None):
    out_dir = dossier_out_dir(project, dossier_id)
    os.makedirs(out_dir, exist_ok=True)
    attempt = int((project.load_state(task).get("monitor_attempt", 0) or 0)) + 1
    model = (cfg.get("monitor") or {}).get("model")
    output_path = os.path.join(out_dir, f"brief-{attempt}.json")
    events_path = os.path.join(out_dir, f"attempt-{attempt}.events.jsonl")
    stderr_path = os.path.join(out_dir, f"attempt-{attempt}.stderr.log")
    stdout_path = os.path.join(out_dir, f"attempt-{attempt}.stdout.json")
    effort = _monitor_effort(cfg, dossier_kind)
    env = dict(os.environ)
    curation = dossier_kind == "curation"
    schema_path = os.path.join(TEMPLATES, "monitor-curation.schema.json" if curation else "monitor-brief.schema.json")
    if model in CONDUCTOR_MODELS:
        env.update({"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    "ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
        command = _monitor_claude_command(project, dossier_id, cfg, effort, schema_path=schema_path)
    else:
        command = _monitor_codex_command(project, dossier_id, cfg, output_path, effort, schema_path=schema_path)
    if leg_n is None:
        state = project.read_state(task, {}) or {}
        leg_n = (current_leg(state) or {}).get("n")
    started = now()
    event_stream = error_stream = None
    stdout = stderr = ""
    try:
        if model in CONDUCTOR_MODELS:
            proc = platform_support.detached_popen(command, cwd=dossier_path(project, dossier_id), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, encoding="utf-8")
        else:
            event_stream = open(events_path, "w", encoding="utf-8")
            error_stream = open(stderr_path, "w", encoding="utf-8")
            proc = platform_support.detached_popen(command, cwd=dossier_path(project, dossier_id), stdin=subprocess.PIPE,
                                    stdout=event_stream, stderr=error_stream, text=True, env=env, encoding="utf-8")
        project.update_state(task, lambda s: _set_monitor_running(s, leg_n, dossier_id, proc.pid, attempt, started))
        stdout, stderr = proc.communicate(prompt, timeout=float((cfg.get("monitor") or {}).get("timeout_minutes", 40)) * 60)
    except subprocess.TimeoutExpired:
        try:
            platform_support.terminate_group(proc)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                platform_support.terminate_group(proc, force=True)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
        if event_stream:
            event_stream.close()
        if error_stream:
            error_stream.close()
        if model in CONDUCTOR_MODELS:
            stdout = stdout or ""
            stderr = stderr or ""
        else:
            stderr = read_text(stderr_path)
        return None, "monitor 超时", attempt, "timeout"
    finally:
        if event_stream:
            event_stream.close()
        if error_stream:
            error_stream.close()
    if model in CONDUCTOR_MODELS:
        with open(stdout_path, "w", encoding="utf-8") as stream:
            stream.write(stdout or "")
        with open(stderr_path, "w", encoding="utf-8") as stream:
            stream.write(stderr or "")
    else:
        stderr = read_text(stderr_path)
    read_error = _monitor_read_error(None, read_text(events_path) + "\n" + stderr + "\n" + (stdout or ""))
    if read_error:
        return None, read_error, attempt, "infrastructure"
    if proc.returncode != 0:
        if is_capacity_failure(events_path, stderr_path, returncode=proc.returncode):
            return None, "monitor 容量不足", attempt, "capacity"
        return None, (stderr or "monitor 进程失败").strip()[-1000:], attempt, "process"
    value, error = _monitor_result_from_files(project, dossier_id, model, output_path, stdout)
    read_error = _monitor_read_error(value)
    if read_error:
        return None, read_error, attempt, "infrastructure"
    if error:
        return None, error, attempt, None
    if curation:
        errors = _validate_curation(value)
    else:
        memory_text = read_text(os.path.join(dossier_path(project, dossier_id), "memory.md"))
        candidate_ids = re.findall(r"^###\s+(C[1-9][0-9]*)\s+·", memory_text, re.MULTILINE)
        errors = _validate_monitor_brief(value, dossier_kind, candidate_ids=candidate_ids)
    if errors:
        return None, "；".join(errors), attempt, None
    return value, "", attempt, None


def run_monitor_check_once(project, task, dossier_id, cfg, prompt, leg_n=None):
    """Run one check monitor process and validate its compact schema."""
    out_dir = dossier_out_dir(project, dossier_id)
    os.makedirs(out_dir, exist_ok=True)
    state = project.load_state(task)
    attempt = int((state.get("monitor_check_attempt", 0) or 0)) + 1
    model = (cfg.get("monitor") or {}).get("model")
    output_path = os.path.join(out_dir, f"check-{attempt}.json")
    events_path = os.path.join(out_dir, f"check-{attempt}.events.jsonl")
    stderr_path = os.path.join(out_dir, f"check-{attempt}.stderr.log")
    stdout_path = os.path.join(out_dir, f"check-{attempt}.stdout.json")
    schema_path = os.path.join(TEMPLATES, "monitor-check.schema.json")
    effort = _monitor_effort(cfg, "report")
    env = dict(os.environ)
    if model in CONDUCTOR_MODELS:
        env.update({"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                    "ENABLE_CLAUDEAI_MCP_SERVERS": "false"})
        command = _monitor_claude_command(project, dossier_id, cfg, effort, schema_path=schema_path)
    else:
        command = _monitor_codex_command(project, dossier_id, cfg, output_path, effort, schema_path=schema_path)
    if leg_n is None:
        leg_n = (current_leg(state) or {}).get("n")
    started = now()
    event_stream = error_stream = None
    stdout = stderr = ""
    try:
        if model in CONDUCTOR_MODELS:
            proc = platform_support.detached_popen(command, cwd=dossier_path(project, dossier_id), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, encoding="utf-8")
        else:
            event_stream = open(events_path, "w", encoding="utf-8")
            error_stream = open(stderr_path, "w", encoding="utf-8")
            proc = platform_support.detached_popen(command, cwd=dossier_path(project, dossier_id), stdin=subprocess.PIPE,
                                    stdout=event_stream, stderr=error_stream, text=True, env=env, encoding="utf-8")
        project.update_state(task, lambda s: _set_check_running(s, leg_n, dossier_id, proc.pid, attempt, started))
        stdout, stderr = proc.communicate(prompt, timeout=float((cfg.get("monitor") or {}).get("timeout_minutes", 40)) * 60)
    except subprocess.TimeoutExpired:
        try:
            platform_support.terminate_group(proc)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                platform_support.terminate_group(proc, force=True)
            except (ProcessLookupError, PermissionError):
                pass
            proc.communicate()
        return None, "轮中检查超时", attempt, "timeout"
    finally:
        if event_stream:
            event_stream.close()
        if error_stream:
            error_stream.close()
    if model in CONDUCTOR_MODELS:
        _dossier_write(stdout_path, stdout or "")
        _dossier_write(stderr_path, stderr or "")
    else:
        stderr = read_text(stderr_path)
    read_error = _monitor_read_error(None, read_text(events_path) + "\n" + stderr + "\n" + (stdout or ""))
    if read_error:
        return None, read_error, attempt, "infrastructure"
    if proc.returncode != 0:
        if is_capacity_failure(events_path, stderr_path, returncode=proc.returncode):
            return None, "轮中检查容量不足", attempt, "capacity"
        return None, (stderr or "轮中检查进程失败").strip()[-1000:], attempt, "process"
    if model in CONDUCTOR_MODELS:
        try:
            wrapper = json.loads(stdout or "")
        except (TypeError, ValueError):
            return None, "Claude 输出不是 JSON", attempt, None
        value = wrapper.get("structured_output") if isinstance(wrapper, dict) else None
    else:
        value = read_json(output_path)
    read_error = _monitor_read_error(value)
    if read_error:
        return None, read_error, attempt, "infrastructure"
    if not isinstance(value, dict):
        return None, "轮中检查没有写出 JSON", attempt, None
    errors = _validate_monitor_check(value, (cfg.get("supervision") or {}).get("check_summary_max_chars", 300))
    if errors:
        return None, "；".join(errors), attempt, None
    return value, "", attempt, None


def _set_check_running(state, leg_n, dossier_id, pid, attempt, started):
    leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
    if leg is not None:
        leg["monitor_check"] = {"id": dossier_id, "status": "running", "pid": pid,
                                 "started_at": started, "attempt": attempt}
    state["monitor_check_attempt"] = attempt


def _check_failure_update(project, task, leg_n, dossier_id, error):
    def update(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is None:
            return
        check = dict(leg.get("monitor_check") or {})
        failures = int(leg.get("monitor_check_failures", 0) or 0) + 1
        check.update({"id": dossier_id, "status": "failed", "pid": None, "ended_at": now(), "error": error})
        leg["monitor_check"] = check
        leg["monitor_check_failures"] = failures
        if failures >= 2:
            state["supervision_fallback"] = True
            leg["supervision_fallback"] = True
    project.update_state(task, update)


def run_monitor_check_lifecycle(project, task, dossier_id, cfg, leg_n):
    base_prompt = read_text(os.path.join(dossier_path(project, dossier_id), "MONITOR_PROMPT.md"))
    retry_cfg = capacity_retry_config(cfg)
    immediate_used = delayed_used = 0
    errors = []
    for schema_attempt in range(2):
        prompt = base_prompt if schema_attempt == 0 else base_prompt + "\n\n---\n\n# 上次输出的校验错误\n" + "\n".join(errors)
        while True:
            good, integrity_error = verify_dossier(project, dossier_id, require_code=True)
            if not good:
                _check_failure_update(project, task, leg_n, dossier_id, integrity_error)
                return False
            value, error, attempt, failure_kind = run_monitor_check_once(project, task, dossier_id, cfg, prompt, leg_n)
            if value is not None:
                return apply_monitor_check_result(project, task, dossier_id, value, leg_n)
            if failure_kind == "capacity":
                phase, delay, immediate_used, delayed_used = next_capacity_retry(
                    retry_cfg, immediate_used, delayed_used)
                if phase and _wait_monitor_check_capacity(project, task, dossier_id, leg_n, delay,
                                                          immediate_used + delayed_used, phase):
                    continue
            errors = [error or "轮中检查失败"]
            if failure_kind == "infrastructure":
                _check_failure_update(project, task, leg_n, dossier_id, error)
                return False
            break
        if not errors:
            break
    _check_failure_update(project, task, leg_n, dossier_id, "；".join(errors))
    return False


def _check_event_record(project, task, dossier_id, value, brief_path=""):
    return append_ledger(project, {"type": "check", "task": task, "dossier": dossier_id,
        "dossier_hash": dossier_manifest(project, dossier_id).get("total_sha256", ""),
        "conclusion": value.get("conclusion"), "summary": value.get("summary", ""),
        "findings": value.get("findings") or [], "brief_path": brief_path})


def _persist_leg_stop_reason(project, task, leg_n, reason):
    """Persist an externally requested stop before interrupting the executor."""
    def update(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is None:
            return
        leg["stop_reason"] = reason
        if reason in {"breaking", "drift_unhandled"}:
            state["supervision_action"] = reason
    project.update_state(task, update)


def apply_monitor_check_result(project, task, dossier_id, value, leg_n):
    """Persist a successful check and apply its conclusion to the running leg."""
    error = _monitor_read_error(value)
    if error:
        _check_failure_update(project, task, leg_n, dossier_id, error)
        return False
    conclusion = value.get("conclusion")
    brief_path = ""
    if conclusion in {"drifting", "breaking"}:
        brief_path = project.p("reviews", dossier_id + "-brief.md")
        os.makedirs(os.path.dirname(brief_path), exist_ok=True)
        with open(brief_path, "w", encoding="utf-8") as stream:
            stream.write(render_monitor_check(project, task, dossier_id, value))
    record = _check_event_record(project, task, dossier_id, value, brief_path)
    def update(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is None:
            return
        check = dict(leg.get("monitor_check") or {})
        check.update({"id": dossier_id, "status": "completed", "pid": None, "ended_at": now(),
                      "conclusion": conclusion, "summary": value.get("summary", ""),
                      "findings": len(value.get("findings") or []), "brief_path": brief_path,
                      "ledger_seq": record.get("seq")})
        leg["monitor_check"] = check
        leg["monitor_check_failures"] = 0
        state["supervision_fallback"] = False
        leg["supervision_fallback"] = False
        if conclusion == "drifting":
            findings = value.get("findings") or []
            state["pending_drift"] = {"dossier": dossier_id, "brief_path": brief_path,
                                       "finding_ids": [item.get("id") for item in findings],
                                       "findings": findings, "summary": value.get("summary", ""), "at": now(),
                                       "deadline": now() + float((config_for_state(project, state).get("supervision") or {}).get("unhandled_stop_minutes", 30)) * 60,
                                       "event_reported": False}
            leg["pending_drift"] = True
        elif conclusion == "breaking":
            state["supervision_action"] = "breaking"
            state["pending_drift"] = None
        else:
            state.pop("supervision_action", None)
        current_interval = float(leg.get("check_interval_minutes") or (config_for_state(project, state).get("supervision") or {}).get("first_check_minutes", 10))
        supervision = config_for_state(project, state).get("supervision") or {}
        if conclusion == "on_track":
            current_interval = min(current_interval * 2, float(supervision.get("max_check_minutes", 60)))
        else:
            current_interval = float(supervision.get("first_check_minutes", 10))
        leg["check_interval_minutes"] = current_interval
        leg["next_check_at"] = now() + current_interval * 60
        leg["last_check"] = {"dossier": dossier_id, "conclusion": conclusion, "at": now(), "brief_path": brief_path}
        leg["check_baseline_ref"] = leg.get("probe_commit") or leg.get("check_baseline_ref")
    project.update_state(task, update)
    if conclusion == "breaking":
        state = project.read_state(task, {}) or {}
        leg = current_leg(state)
        if leg:
            _persist_leg_stop_reason(project, task, leg_n, "breaking")
            with open(project.leg_path(task, leg_n, "stop"), "w", encoding="utf-8") as stream:
                stream.write("breaking")
            state = project.read_state(task, {}) or state
            leg = current_leg(state)
            kill_codex(leg)
    return True


def monitor_check_due(st, leg, now_value=None):
    if not leg or leg.get("status") != "running":
        return False
    if (leg.get("monitor_check") or {}).get("status") == "running":
        return False
    now_value = now() if now_value is None else float(now_value)
    if leg.get("check_pending"):
        return True
    return now_value >= float(leg.get("next_check_at", float("inf")))


def maybe_start_monitor_check(project, task, st, leg, force=False):
    """Schedule one isolated check process without blocking the executor runner."""
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg) or cfg.get("mode") == "lean" or not leg or leg.get("status") != "running":
        return False
    if force:
        first_interval = float((cfg.get("supervision") or {}).get("first_check_minutes", 10))
        project.update_state(task, lambda state: current_leg(state).update(
            check_interval_minutes=first_interval, next_check_at=now() + first_interval * 60))
        st = project.load_state(task)
        leg = current_leg(st)
    check = leg.get("monitor_check") or {}
    if check.get("status") == "running":
        if force or leg.get("check_pending"):
            project.update_state(task, lambda state: current_leg(state).update(check_pending=True))
        return False
    due = force or monitor_check_due(st, leg)
    if not due:
        return False
    current = project.load_state(task)
    dossier_id = build_check_dossier(project, task, current, current_leg(current))
    if not dossier_id:
        return False
    project.update_state(task, lambda state: current_leg(state).update(check_pending=False))
    log_path = os.path.join(dossier_out_dir(project, dossier_id), "check.runner.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    try:
        runner = platform_support.detached_popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                   "_monitor_check", task, dossier_id], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, cwd=project.root)
    finally:
        log.close()
    project.update_state(task, lambda state: (current_leg(state).setdefault("monitor_check", {}).update(
        pid=runner.pid, marker=f"_monitor_check {task} {dossier_id}")))
    _DETACHED_RUNNERS.append(runner)
    return True


def cmd_monitor_check_run(project, args):
    st = project.load_state(args.task)
    leg = current_leg(st)
    if not leg:
        return
    cfg = config_for_state(project, st)
    run_monitor_check_lifecycle(project, args.task, args.dossier, cfg, leg.get("n"))


def _set_monitor_running(state, leg_n, dossier_id, pid, attempt, started):
    if str(dossier_id).startswith(str(state.get("task", "")) + "-curate-"):
        state["curation_monitor"] = {"id": dossier_id, "status": "running", "pid": pid,
                                      "started_at": started, "attempt": attempt,
                                      "marker": f"_monitor_run {state.get('task', '')} {dossier_id}"}
        state["monitor_attempt"] = attempt
        return
    leg = next((x for x in state.get("legs", []) if x.get("n") == leg_n), None)
    if leg is not None:
        previous = leg.get("monitor") or {}
        monitor_pid = pid
        marker = previous.get("marker")
        if marker and previous.get("pid"):
            monitor_pid = previous["pid"]
        elif state.get("task") and leg.get("runner_pid"):
            monitor_pid = leg["runner_pid"]
            marker = runner_marker(state["task"], leg_n)
        monitor = {"id": dossier_id, "status": "running", "pid": monitor_pid,
                   "started_at": started, "attempt": attempt}
        if marker:
            monitor["marker"] = marker
        if previous.get("rerun"):
            monitor["rerun"] = True
        leg["monitor"] = monitor
    state["monitor_attempt"] = attempt


def _monitor_state_update(project, task, dossier_id, leg_n, status, **extra):
    def update(s):
        if str(dossier_id).startswith(str(task) + "-curate-"):
            monitor = dict(s.get("curation_monitor") or {})
            if status == "failed" and monitor.get("status") == "completed":
                return
            monitor.update({"id": dossier_id, "status": status, "pid": None, "ended_at": now()})
            monitor.update(extra)
            if status == "completed":
                monitor.pop("error", None)
            s["curation_monitor"] = monitor
            return
        leg = next((x for x in s.get("legs", []) if x.get("n") == leg_n), None)
        if leg is None:
            return
        monitor = dict(leg.get("monitor") or {})
        if status == "failed" and monitor.get("status") == "completed":
            return
        monitor.update({"id": dossier_id, "status": status, "pid": None, "ended_at": now()})
        monitor.update(extra)
        if status == "completed":
            monitor.pop("error", None)
        leg["monitor"] = monitor
    project.update_state(task, update)


def _monitor_wait_update(project, task, dossier_id, leg_n, wait):
    def update(s):
        leg = next((x for x in s.get("legs", []) if x.get("n") == leg_n), None)
        if leg is not None:
            monitor = dict(leg.get("monitor") or {})
            monitor["id"] = dossier_id
            monitor["status"] = "running"
            monitor["capacity_wait"] = wait
            leg["monitor"] = monitor
    project.update_state(task, update)


def _monitor_wait_clear(project, task, dossier_id, leg_n):
    def update(s):
        leg = next((x for x in s.get("legs", []) if x.get("n") == leg_n), None)
        if leg is not None:
            monitor = dict(leg.get("monitor") or {})
            monitor.pop("capacity_wait", None)
            leg["monitor"] = monitor
    project.update_state(task, update)


def _wait_monitor_capacity(project, task, dossier_id, leg_n, delay, retry_no, phase):
    wait = {"phase": phase, "retry": retry_no, "started_at": now(),
            "next_retry_at": now() + max(0.0, delay)}
    _monitor_wait_update(project, task, dossier_id, leg_n, wait)
    stop_flag = project.leg_path(task, leg_n, "stop")
    while now() < wait["next_retry_at"]:
        if os.path.exists(stop_flag):
            _monitor_wait_clear(project, task, dossier_id, leg_n)
            return False
        time.sleep(min(0.2, max(0.01, wait["next_retry_at"] - now())))
    _monitor_wait_clear(project, task, dossier_id, leg_n)
    return True


def _wait_monitor_check_capacity(project, task, dossier_id, leg_n, delay, retry_no, phase):
    wait = {"phase": phase, "retry": retry_no, "started_at": now(),
            "next_retry_at": now() + max(0.0, delay)}
    def mark(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is not None:
            check = dict(leg.get("monitor_check") or {})
            check["capacity_wait"] = wait
            leg["monitor_check"] = check
    project.update_state(task, mark)
    stop_flag = project.leg_path(task, leg_n, "stop")
    while now() < wait["next_retry_at"]:
        if os.path.exists(stop_flag):
            return False
        time.sleep(min(0.2, max(0.01, wait["next_retry_at"] - now())))
    def clear(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is not None:
            check = dict(leg.get("monitor_check") or {})
            check.pop("capacity_wait", None)
            leg["monitor_check"] = check
    project.update_state(task, clear)
    return True


def _monitor_capacity_error(text):
    try:
        quota = safe_quota(block=False)
    except Exception:  # quota reporting must not hide the monitor failure
        quota = None
    if isinstance(quota, dict):
        reset = quota.get("reset_at") or quota.get("reset_time") or quota.get("resets_at")
        if reset:
            return f"{text}；额度重置时间：{reset}"
    return text


def run_monitor_lifecycle(project, task, dossier_id, dossier_kind, cfg, leg_n, original_status):
    """Run the isolated monitor, retrying one invalid response and recording its verdict."""
    base_prompt = read_text(os.path.join(dossier_path(project, dossier_id), "MONITOR_PROMPT.md"))
    prompt = base_prompt
    errors = []
    retry_cfg = capacity_retry_config(cfg)
    immediate_used = delayed_used = 0
    schema_attempt = 0
    while schema_attempt < 2:
        prompt = base_prompt if schema_attempt == 0 else base_prompt + "\n\n---\n\n# 上次输出的校验错误（由 baton 添加）\n" + "\n".join(errors)
        capacity_error = None
        terminal_error = None
        while True:
            good, integrity_error = verify_dossier(project, dossier_id, require_code=True)
            if not good:
                _monitor_state_update(project, task, dossier_id, leg_n, "failed", error=integrity_error)
                return False
            brief, error, attempt, failure_kind = run_monitor_once(project, task, dossier_id, dossier_kind, cfg, prompt, leg_n)
            if brief is None and failure_kind in ("timeout", "process", "infrastructure"):
                terminal_error = error or "monitor 进程失败"
                break
            if brief is None and failure_kind == "capacity":
                phase, delay, immediate_used, delayed_used = next_capacity_retry(
                    retry_cfg, immediate_used, delayed_used)
                if phase:
                    retry_no = immediate_used + delayed_used
                    if not _wait_monitor_capacity(project, task, dossier_id, leg_n, delay, retry_no, phase):
                        capacity_error = "monitor 容量重试被停止"
                        break
                    continue
                capacity_error = "monitor 容量不足，重试次数已用完"
            break
        if capacity_error:
            errors = [_monitor_capacity_error(capacity_error)]
            break
        if terminal_error:
            errors = [terminal_error]
            break
        if brief is not None:
            good, integrity_error = verify_dossier(project, dossier_id, require_code=True)
            if not good:
                _monitor_state_update(project, task, dossier_id, leg_n, "failed", error=integrity_error)
                return False
            legs_for_rerun = (project.read_state(task, {}) or {}).get("legs") or [{}]
            rerun = bool((legs_for_rerun[-1] or {}).get("monitor", {}).get("rerun"))
            brief_text = render_monitor_brief(project, task, dossier_id, brief, rerun=rerun)
            review_path = project.p("reviews", dossier_id + "-brief.md")
            os.makedirs(os.path.dirname(review_path), exist_ok=True)
            with open(review_path, "w", encoding="utf-8") as stream:
                stream.write(brief_text)
            manifest = dossier_manifest(project, dossier_id)
            record = append_brief_ledger(project, task, dossier_id, manifest.get("total_sha256", ""),
                                         brief_text, rerun=rerun, brief_json=brief)
            _monitor_state_update(project, task, dossier_id, leg_n, "completed", brief_path=review_path,
                                  recommendation=brief.get("recommendation"), summary=brief.get("summary", ""),
                                  findings=len(brief.get("findings") or []),
                                  blocking=sum(1 for x in brief.get("findings") or [] if x.get("severity") == "blocking"),
                                  note=sum(1 for x in brief.get("findings") or [] if x.get("severity") == "note"),
                                  ledger_seq=record.get("seq"))
            return True
        errors = [error or "monitor 没有交出有效简报"]
        schema_attempt += 1
    _monitor_state_update(project, task, dossier_id, leg_n, "failed", error="；".join(errors))
    return False


def render_monitor_brief(project, task, dossier_id, brief, rerun=False):
    manifest = dossier_manifest(project, dossier_id) if os.path.isdir(dossier_path(project, dossier_id)) else {}
    if manifest.get("type") == "curation":
        lines = [f"# 记忆整理简报 · {dossier_id}", "", f"任务：`{task}`", "", "## 摘要", brief.get("summary", ""), "", "## 操作"]
        operations = brief.get("operations") or []
        if not operations:
            lines.append("无")
        for item in operations:
            lines.append("- %s · %s · %s · %s" %
                         (item.get("id"), item.get("op"), ",".join(item.get("targets") or []), item.get("text", "")))
            lines.append("  来源：%s；锚点：%s；理由：%s" %
                         (item.get("source"), ", ".join(item.get("anchors") or []), item.get("reason")))
        return "\n".join(lines) + "\n"
    lines = [f"# 监督简报 · {dossier_id}", "", f"任务：`{task}`", f"建议结论：**{brief['recommendation']}**", "",
             "## 摘要", brief["summary"], "", "## 偏离"]
    findings = brief.get("findings") or []
    if not findings:
        lines.append("无")
    for finding in findings:
        lines += [f"### {finding['id']} · {finding['category']} · {finding['severity']}", finding["description"],
                  f"规则出处：{finding['rule_source']}"]
        for ev in finding.get("evidence") or []:
            lines.append(f"- {ev['location']}：{ev['excerpt']}")
    lines += ["", "## 记忆建议"]
    advice = brief.get("memory_advice") or []
    if not advice:
        lines.append("无")
    else:
        for item in advice:
            lines.append("- %s · %s · %s · %s" %
                         (item.get("id"), item.get("action"), item.get("kind"), item.get("text")))
            lines.append("  来源：%s；锚点：%s；理由：%s" %
                         (item.get("source"), ", ".join(item.get("anchors") or []), item.get("reason")))
    if rerun:
        lines += ["", "本简报由 `--rerun` 产生。"]
    return "\n".join(lines) + "\n"


def render_monitor_check(project, task, dossier_id, value):
    lines = [f"# 轮中检查 · {dossier_id}", "", f"任务：`{task}`", f"结论：**{value['conclusion']}**", "",
             "## 摘要", value.get("summary", ""), "", "## 发现"]
    findings = value.get("findings") or []
    if not findings:
        lines.append("无")
    for finding in findings:
        lines += [f"### {finding['id']} · {finding['category']} · {finding['severity']}", finding["description"],
                  f"规则出处：{finding['rule_source']} "]
        lines.extend(f"- {item['location']}：{item['excerpt']}" for item in finding.get("evidence") or [])
    return "\n".join(lines) + "\n"


def _check_baseline_ref(st, leg):
    return leg.get("check_baseline_ref") or leg.get("start_ref") or st.get("base_ref")


def _check_evidence_text(project, task, st, leg, baseline, probe_ref):
    store = project.store(task, st)
    lines = ["# 轮中证据", "", "## Diff"]
    if store and baseline and probe_ref:
        lines.append(store.git("diff", "--stat=120", baseline, probe_ref, "--", ".", *code_excludes(store), check=False).rstrip() or "（无）")
        lines.append("\n按文件摘要：")
        for _, _, status, path in diff_tree_entries(store, baseline, probe_ref):
            lines.append(f"- {status} {path}")
    else:
        lines.append("（无影子仓库快照）")
    lines += ["", "## ALERT"]
    alert_lines = [f"- {item.get('id')} · {item.get('rule')} · {item.get('category')}：{item.get('evidence')}"
                   for item in leg.get("alerts") or []]
    lines.extend(alert_lines or ["无"])
    lines += ["", "## 终端调用"]
    calls = leg_terminal_calls(project, task, leg, since=leg.get("last_check_terminal_at"))
    lines.extend([f"- [{item.get('exit_code')}] {item.get('command')}"
                  + (f"（raw: {item.get('raw')}）" if item.get("raw") and item.get("raw") != item.get("command") else "")
                  + f" @ {item.get('time')}" for item in calls] or ["无"])
    lines += ["", "## 冻结检查", freeze_summary(check_freeze(project, task, st)) or "无"]
    return "\n".join(lines) + "\n"


def build_check_dossier(project, task, st, leg):
    """Build and seal a read-only dossier for one asynchronous direction check."""
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg) or cfg.get("mode") == "lean":
        return None
    store = project.store(task, st)
    probe_ref = leg.get("probe_ref")
    if not store or not probe_ref:
        return None
    seq = int(leg.get("check_seq", 0) or 0) + 1
    dossier_id = f"{task}-leg-{leg['n']:03d}-c{seq:02d}"
    root = dossier_path(project, dossier_id)
    prefix = f"{task}-leg-{leg['n']:03d}-c"
    base = os.path.join(project.state_dir, "dossiers")
    if os.path.isdir(base):
        for previous in sorted(os.listdir(base)):
            if previous != dossier_id and re.fullmatch(re.escape(prefix) + r"\d+", previous):
                _prune_dossier_code(project, previous, "同一轮已建立新的检查卷宗")
    if os.path.exists(root):
        _unseal_dossier(root)
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    baseline = _check_baseline_ref(st, leg)
    prompt = fill(read_text(os.path.join(TEMPLATES, "monitor-check-prompt.md")),
                  {"TASK": task, "DOSSIER": dossier_id})
    _dossier_write(os.path.join(root, "MONITOR_PROMPT.md"), prompt)
    _dossier_write(os.path.join(root, "charter.md"), _charter_text(project, task, st))
    _dossier_write(os.path.join(root, "precedents.md"), _precedents_text(project, task))
    _dossier_write(os.path.join(root, "evidence.md"), _check_evidence_text(project, task, st, leg, baseline, probe_ref))
    diff_text = store.git("diff", "--binary", baseline, probe_ref, "--", ".", *code_excludes(store), check=False) if baseline else ""
    _dossier_write(os.path.join(root, "diff.patch"), diff_text)
    omissions = _dossier_code_snapshot(store, probe_ref, os.path.join(root, "code"))
    files = _dossier_files(root)
    write_json(os.path.join(root, "manifest.json"), {"dossier": dossier_id, "task": task, "leg": leg.get("n"),
        "type": "check", "code_omissions": omissions, "files": files,
        "total_sha256": _dossier_total(files), "created_at": now()})
    _seal_dossier(root)
    def mark(state):
        current = current_leg(state)
        if current and current.get("n") == leg["n"]:
            current.update(check_seq=seq, check_baseline_ref=leg.get("probe_commit") or probe_ref, check_dossier=dossier_id,
                          last_check_terminal_at=leg.get("last_rule_terminal_at"), check_started_at=now())
    project.update_state(task, mark)
    return dossier_id


VERDICT_CONCLUSIONS = {
    "report": {"APPROVE", "REVISE", "ROLLBACK"},
    "final": {"ACCEPT", "REVISE"},
    "violation": {"REVERT", "ADOPT", "ASK_USER"},
    "seed": {"APPROVE", "REVISE"},
}


def _brief_findings(brief_text="", brief_json=None):
    """Return the machine-relevant finding metadata from a rendered brief."""
    if isinstance(brief_json, dict) and isinstance(brief_json.get("findings"), list):
        result = {}
        for item in brief_json["findings"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                result[item["id"]] = {"category": item.get("category", ""),
                                       "severity": item.get("severity", "")}
        return result
    findings = {}
    for match in re.finditer(r"^###\s+(F\d+)\s+·\s+([^·\n]+?)\s+·\s+([^\n]+)$",
                             brief_text or "", re.MULTILINE):
        findings[match.group(1)] = {"category": match.group(2).strip(),
                                    "severity": match.group(3).strip()}
    return findings


def _parse_verdict(text, dossier_kind, brief_text="", brief_json=None):
    """Parse and validate the leading baton-verdict JSON fence."""
    match = re.match(r"\A\s*```baton-verdict\s*\n(.*?)\n```(?:\s*\n|\s*\Z)",
                     text or "", re.DOTALL)
    if not match:
        raise ConfigError("裁决文件开头必须有 baton-verdict JSON 围栏块")
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError(f"重复字段 {key}")
            obj[key] = value
        return obj
    try:
        value = json.loads(match.group(1), object_pairs_hook=unique_object)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"baton-verdict 不是合法 JSON：{exc}") from exc
    if not isinstance(value, dict) or not set(value).issubset({"conclusion", "findings", "memory"}) \
            or not {"conclusion", "findings"}.issubset(set(value)):
        raise ConfigError("baton-verdict 必须包含 conclusion、findings，可选 memory")
    conclusion = value.get("conclusion")
    if not isinstance(conclusion, str) or conclusion not in VERDICT_CONCLUSIONS.get(dossier_kind, set()):
        raise ConfigError(f"结论 {conclusion!r} 不适用于 {dossier_kind} 卷宗")
    findings = value.get("findings")
    if not isinstance(findings, dict):
        raise ConfigError("findings 必须是对象")
    expected = _brief_findings(brief_text, brief_json)
    if set(findings) != set(expected):
        missing = sorted(set(expected) - set(findings))
        extra = sorted(set(findings) - set(expected))
        detail = []
        if missing:
            detail.append("漏答 " + ", ".join(missing))
        if extra:
            detail.append("多答 " + ", ".join(extra))
        raise ConfigError("裁决必须逐条回应简报偏离（" + "；".join(detail) + "）")
    for fid, response in findings.items():
        if not isinstance(response, dict) or set(response) != {"decision", "reason"}:
            raise ConfigError(f"{fid} 必须是只含 decision 和 reason 的对象")
        if not isinstance(response.get("decision"), str) or response.get("decision") not in {"adopt", "reject"}:
            raise ConfigError(f"{fid}.decision 只能是 adopt 或 reject")
        if not isinstance(response.get("reason"), str) or not response["reason"].strip():
            raise ConfigError(f"{fid}.reason 不能为空")
    memory = value.get("memory", {})
    if not isinstance(memory, dict):
        raise ConfigError("memory 必须是对象")
    for ident, choice in memory.items():
        if not isinstance(ident, str) or not re.fullmatch(r"C[1-9][0-9]*|M[0-9]*[1-9][0-9]*|new-[1-9][0-9]*", ident):
            raise ConfigError("memory 键必须是 C/M 编号或 new-N")
        if (not isinstance(choice, str) or choice not in {"approve", "reject"}) and not (
                isinstance(choice, dict) and set(choice).issubset({"text", "pinned"})
                and "text" in choice and isinstance(choice.get("text"), str) and choice["text"].strip()
                and ("pinned" not in choice or isinstance(choice.get("pinned"), bool))):
            raise ConfigError("memory.%s 必须是 approve、reject 或 {text: ...}" % ident)
    return value, match.end()


def _parse_curation_verdict(text, brief_json=None):
    match = re.match(r"\A\s*```baton-verdict\s*\n(.*?)\n```(?:\s*\n|\s*\Z)", text or "", re.DOTALL)
    if not match:
        raise ConfigError("整理裁决文件开头必须有 baton-verdict JSON 围栏块")
    try:
        value = json.loads(match.group(1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("整理裁决 JSON 无法解析：%s" % exc) from exc
    if not isinstance(value, dict) or set(value) - {"operations", "conclusion"} or "operations" not in value:
        raise ConfigError("整理裁决只能包含 operations，可选 conclusion")
    operations = value["operations"]
    if not isinstance(operations, dict):
        raise ConfigError("整理裁决 operations 必须是对象")
    expected = {item.get("id") for item in (brief_json or {}).get("operations", []) if isinstance(item, dict)}
    if expected and set(operations) != expected:
        raise ConfigError("整理裁决必须逐条回应每个 K 操作")
    for ident, decision in operations.items():
        if not re.fullmatch(r"K[1-9][0-9]*", str(ident)):
            raise ConfigError("整理裁决键必须是 K 编号")
        if decision not in {"approve", "reject"}:
            raise ConfigError("整理裁决 %s 只能是 approve 或 reject" % ident)
    return value, match.end()


def _apply_curation_operations(project, task, dossier_id, brief_json, decisions):
    from baton_memory.ledger import MemoryLedger, memory_append_approved
    ledger = MemoryLedger(project)
    latest = ledger.latest()
    written = []
    accepted = []
    for operation in (brief_json or {}).get("operations", []):
        if not isinstance(operation, dict) or decisions.get(operation.get("id")) != "approve":
            continue
        op = operation.get("op")
        targets = operation.get("targets") or []
        if any(target not in latest for target in targets):
            raise ConfigError("整理操作目标不存在：" + ", ".join(targets))
        if op in {"merge", "supersede"}:
            entry = {"kind": operation.get("kind"), "text": operation.get("text"),
                     "source": dossier_id, "source_actor": "monitor", "origin": operation.get("source"),
                     "anchors": operation.get("anchors"), "task": task, "leg": None,
                     "end_snapshot": "", "curation_id": operation.get("id"), "verified": True}
            new = memory_append_approved(ledger, [entry], source=dossier_id, mem0_writer=None)[0]
            written.append(new)
            for target in targets:
                ledger.append_status(target, "superseded", replaced_by=new["id"],
                                      status_reason="已被 %s 取代" % new["id"], curation_id=operation.get("id"))
        elif op == "pin":
            for target in targets:
                ledger.append_status(target, latest[target].get("status", "active"), pinned=True,
                                      pin_seq=latest[target].get("seq", 0), curation_id=operation.get("id"))
        elif op == "unpin":
            for target in targets:
                ledger.append_status(target, latest[target].get("status", "active"), pinned=False,
                                      curation_id=operation.get("id"))
        elif op == "retire":
            for target in targets:
                ledger.append_status(target, "retired", status_reason=operation.get("reason", "整理退役"),
                                      curation_id=operation.get("id"))
        accepted.append(operation.get("id"))
    return written, accepted


def _latest_ledger_record(records, kind, task, dossier=None, decision_id=None):
    matches = [r for r in records if r.get("type") == kind and r.get("task") == task]
    if dossier is not None:
        matches = [r for r in matches if r.get("dossier") == dossier]
    if decision_id is not None:
        matches = [r for r in matches if r.get("decision_id") == decision_id]
    return matches[-1] if matches else None


def _dossier_leg(project, task, dossier_id, st=None):
    st = st or project.load_state(task)
    for leg in reversed(st.get("legs", [])):
        if (leg.get("dossier") or {}).get("id") == dossier_id or (leg.get("monitor") or {}).get("id") == dossier_id:
            return leg
    return None


def _dossier_brief(project, task, dossier_id, manifest=None):
    manifest = manifest or dossier_manifest(project, dossier_id)
    dossier_hash = manifest.get("total_sha256") if isinstance(manifest, dict) else None
    records = ledger_records(project)
    latest = _latest_ledger_record(records, "brief", task, dossier=dossier_id)
    return (latest if latest and latest.get("dossier_hash") == dossier_hash else None), dossier_hash


def _verdict_render(dossier_id, value, body, brief_text="", brief_json=None):
    lines = [f"# 裁决 · {dossier_id}", "", f"结论：**{value['conclusion']}**", "", "## 逐条回应"]
    metadata = _brief_findings(brief_text, brief_json)
    if not value["findings"]:
        lines.append("无偏离")
    for fid, response in value["findings"].items():
        meta = metadata.get(fid, {})
        lines += [f"### {fid} · {meta.get('category', 'unknown')} · {meta.get('severity', 'unknown')}",
                  f"决定：**{response['decision']}**", f"理由：{response['reason']}", ""]
    if value.get("memory"):
        lines += ["## 记忆逐条覆盖"]
        for ident, choice in value["memory"].items():
            lines.append("- %s：%s" % (ident, json.dumps(choice, ensure_ascii=False, sort_keys=True)))
    lines += ["## 给执行者的指令", (body.strip() or "（无）"), ""]
    return "\n".join(lines).rstrip() + "\n"


def _verdict_for_dossier(project, task, dossier_id):
    if not verify_ledger(project):
        return None
    return _latest_ledger_record(ledger_records(project), "verdict", task, dossier=dossier_id)


def _revision_key(project, dossier_id):
    report = read_text(os.path.join(dossier_path(project, dossier_id), "report.html"))
    match = re.search(r"修订自\s*(R\d{3})", report or "")
    return match.group(1) if match else None


def _revision_counts(project, task):
    """Count current REVISE verdicts by the report item they revise.

    A rerule replaces the effective verdict for a dossier; the ledger remains append-only
    but the count is derived from the latest record for each dossier.
    """
    if not verify_ledger(project):
        return {}
    latest = {}
    for record in ledger_records(project):
        if record.get("type") == "verdict" and record.get("task") == task:
            latest[record.get("dossier")] = record
    counts = {}
    for dossier, record in latest.items():
        if record.get("conclusion") != "REVISE":
            continue
        key = _revision_key(project, dossier) or dossier
        counts[key] = counts.get(key, 0) + 1
    return counts


def _revision_count(project, task, dossier_id=None):
    counts = _revision_counts(project, task)
    key = (_revision_key(project, dossier_id) if dossier_id else None) or dossier_id
    return int(counts.get(key, 0) or 0) if key else 0


# ---------------------------------------------------------------- executor escalation

_ESCALATION_NON_METHOD_KINDS = {
    "capacity", "provider", "provider_overload", "quota", "network", "permission",
    "user_input", "user", "timeout", "credentials", "auth", "external",
}
_ESCALATION_NON_METHOD_RE = re.compile(
    r"(?:at\s+capacity|capacity|503|429|rate.?limit|quota|额度|过载|网络|network|"
    r"权限|permission|forbidden|缺少.*权限|用户输入|需要用户|请用户|auth|登录)", re.I)
_ESCALATION_METHOD_RE = re.compile(
    r"(?:方法卡住|method(?:\s+|-)stuck|implementation|实现(?:仍|上)?卡住|修复失败|"
    r"同一问题|无法完成(?:该|此)?(?:方法|实现)|反复失败|approach.*stuck)", re.I)


def _leg_failure_text(leg):
    result = leg.get("result") if isinstance(leg, dict) and isinstance(leg.get("result"), dict) else {}
    return " ".join(str(value) for value in (
        leg.get("failure_kind", ""), leg.get("error", ""), result.get("summary", ""),
        result.get("failure_kind", "")) if value)


def method_stuck_failure(leg):
    """Classify a blocked/failed leg for automatic escalation.

    Explicit failure_kind values are authoritative.  Provider, quota, network,
    permission and user-input failures are deliberately excluded; only an explicit
    method-stuck kind or a summary that says the implementation is stuck counts.
    """
    if not isinstance(leg, dict) or leg.get("status") not in {"blocked", "failed"}:
        return False
    kind = str(leg.get("failure_kind") or "").strip().lower().replace("-", "_")
    if kind in _ESCALATION_NON_METHOD_KINDS:
        return False
    if kind in {"method_stuck", "method", "implementation", "approach"}:
        return True
    text = _leg_failure_text(leg)
    if _ESCALATION_NON_METHOD_RE.search(text):
        return False
    return bool(_ESCALATION_METHOD_RE.search(text))


def _verdict_conclusions_for_task(project, task):
    if not verify_ledger(project):
        return []
    return [str(record.get("conclusion", "")).upper()
            for record in ledger_records(project)
            if record.get("type") == "verdict" and record.get("task") == task
            and str(record.get("conclusion", "")).upper() in {"REVISE", "APPROVE", "ACCEPT"}]


def escalation_counters(project, task, st=None):
    """Return the current automatic escalation counters for a task."""
    st = st or project.read_state(task, {}) or {}
    revise_streak = 0
    for conclusion in _verdict_conclusions_for_task(project, task):
        if conclusion == "REVISE":
            revise_streak += 1
        else:
            revise_streak = 0
    blocked_streak = 0
    for leg in reversed(st.get("legs", [])):
        status = leg.get("status")
        if status in {"blocked", "failed"}:
            if method_stuck_failure(leg):
                blocked_streak += 1
                continue
            # Known external failures are neutral: they do not make the method look
            # stuck, but they also do not erase a preceding method failure.
            if str(leg.get("failure_kind") or "").lower() in _ESCALATION_NON_METHOD_KINDS:
                continue
            if _ESCALATION_NON_METHOD_RE.search(_leg_failure_text(leg)):
                continue
            break
        # A successful or decision/report leg separates method failures.
        break
    escalated = sum(1 for leg in st.get("legs", []) if leg.get("escalated"))
    state_escalation = st.get("escalation") if isinstance(st.get("escalation"), dict) else {}
    try:
        auto_count = int(state_escalation.get("auto_count", escalated) or 0)
    except (TypeError, ValueError):
        auto_count = escalated
    return {"revise_streak": revise_streak, "method_stuck_streak": blocked_streak,
            "auto_count": auto_count,
            "suppressed": bool(state_escalation.get("auto_suppressed", False))}


def escalation_decision(project, task, st=None, force=False):
    """Choose the model for the next leg without mutating the task state."""
    st = st or project.read_state(task, {}) or {}
    cfg = config_for_state(project, st)
    base = cfg.get("executor") or {}
    escalation = base.get("escalation") or {}
    if cfg.get("mode") == "lean" or not escalation.get("enabled", True):
        return {"enabled": False, "escalated": False, "reason": "lean/disabled"}
    counters = escalation_counters(project, task, st)
    try:
        max_per_task = int(escalation.get("max_per_task", 3))
    except (TypeError, ValueError):
        max_per_task = 3
    if force:
        return {"enabled": True, "escalated": True, "reason": "conductor --escalate",
                "auto": False, "auto_count": counters["auto_count"]}
    if counters["suppressed"]:
        return {"enabled": True, "escalated": False,
                "reason": "上次升级后仍未通过，等待 conductor 决定继续用 Sol、拆分任务或问用户",
                "auto": False, "auto_count": counters["auto_count"]}
    if counters["auto_count"] >= max_per_task:
        return {"enabled": True, "escalated": False,
                "reason": f"本任务自动升级已达到上限 {max_per_task} 次，请 conductor 决定",
                "auto": False, "auto_count": counters["auto_count"]}
    after_revise = int(escalation.get("after_revise", 2))
    after_blocked = int(escalation.get("after_blocked_or_failed_legs", 2))
    reasons = []
    if counters["revise_streak"] >= after_revise:
        reasons.append(f"连续 REVISE {counters['revise_streak']} 次")
    if counters["method_stuck_streak"] >= after_blocked:
        reasons.append(f"连续方法卡住轮次 {counters['method_stuck_streak']} 次")
    if reasons:
        return {"enabled": True, "escalated": True, "reason": "；".join(reasons),
                "auto": True, "auto_count": counters["auto_count"] + 1}
    return {"enabled": True, "escalated": False, "reason": "默认模型", "auto": False,
            "auto_count": counters["auto_count"]}


def _effective_executor_config(cfg, decision):
    effective = json.loads(json.dumps(cfg))
    if decision.get("escalated"):
        escalation = effective["executor"].get("escalation") or {}
        effective["executor"]["model"] = escalation.get("model", effective["executor"].get("model"))
        effective["executor"]["reasoning_effort"] = escalation.get(
            "reasoning_effort", effective["executor"].get("reasoning_effort"))
    return effective


def _rule_conclusion_hint(task, conclusion, dossier_id, leg):
    if conclusion == "ROLLBACK":
        return "ROLLBACK：先向用户说明原因和回滚点；用户同意后运行 baton rollback，若不同意则用 --rerule 改成 REVISE。"
    if conclusion == "ADOPT":
        paths = sorted({x.get('path') for x in leg.get('violations', []) if isinstance(x, dict) and x.get('path')})
        args = " ".join("--adopt " + shlex.quote(path) for path in paths)
        return f"ADOPT：用户要在自己的终端运行 baton amend {task} {args} --user-approved。"
    if conclusion == "REVERT":
        return f"REVERT：请运行 baton restore-frozen {task}，恢复完成后再 resume。"
    if conclusion == "ASK_USER":
        return "ASK_USER：resume 已拦截，等用户决定后用 baton rule --rerule 改成 REVERT 或 ADOPT。"
    return f"下一步：baton resume {task} --verdict {dossier_id}"


def cmd_rule(project, args):
    project.require_init()
    check_task(args.task)
    if not verify_ledger(project):
        die("账本校验失败，拒绝入账")
    good, error = verify_dossier(project, args.dossier)
    if not good:
        die("卷宗校验失败：" + error)
    manifest = dossier_manifest(project, args.dossier)
    kind = manifest.get("type")
    if kind == "curation":
        brief_record, dossier_hash = _dossier_brief(project, args.task, args.dossier, manifest)
        if not brief_record:
            die("找不到绑定这份整理卷宗且哈希一致的简报")
        if not os.path.isfile(args.verdict_file):
            die(f"找不到裁决文件 {args.verdict_file}")
        value, fence_end = _parse_curation_verdict(read_text(args.verdict_file), brief_record.get("brief_json"))
        prior = _latest_ledger_record(ledger_records(project), "curation", args.task, dossier=args.dossier)
        if prior and not getattr(args, "rerule", False):
            die(f"整理卷宗 {args.dossier} 已有裁决，重裁决请加 --rerule")
        decisions = value.get("operations", {})
        written, accepted = _apply_curation_operations(project, args.task, args.dossier,
                                                        brief_record.get("brief_json"), decisions)
        record = append_ledger(project, {"type": "curation", "task": args.task, "dossier": args.dossier,
            "dossier_hash": dossier_hash, "brief_seq": brief_record.get("seq"),
            "operations": decisions, "accepted": accepted, "written": [x.get("id") for x in written],
            "verdict": read_text(args.verdict_file)})
        review_path = project.p("reviews", args.dossier + "-verdict.md")
        os.makedirs(os.path.dirname(review_path), exist_ok=True)
        with open(review_path, "w", encoding="utf-8") as stream:
            stream.write("# 整理裁决 · %s\n\n" % args.dossier)
            stream.write("已核准操作：" + (", ".join(accepted) or "无") + "\n")
        project.update_state(args.task, lambda s: s.update(curation_monitor={"id": args.dossier,
            "status": "ruled", "verdict_seq": record.get("seq"), "brief_path": review_path}))
        print(f"已入账整理裁决 {args.dossier}（ledger seq {record['seq']}）")
        if written:
            print("整理新增记忆：" + ", ".join(x.get("id", "?") for x in written))
        return
    if kind not in VERDICT_CONCLUSIONS:
        die("卷宗类型无效，无法裁决")
    st = project.load_state(args.task)
    leg = _dossier_leg(project, args.task, args.dossier, st)
    if not leg:
        die(f"找不到卷宗 {args.dossier} 对应的任务轮次")
    monitor = leg.get("monitor") or {}
    if monitor.get("status") == "running":
        die("monitor 仍在运行，请等待监督简报完成")
    if monitor.get("status") == "failed":
        die("monitor 已失败，可以等待、baton monitor --rerun 或问用户")
    brief_record, dossier_hash = _dossier_brief(project, args.task, args.dossier, manifest)
    if not brief_record:
        die("找不到绑定这份卷宗且哈希一致的最新监督简报")
    if not os.path.isfile(args.verdict_file):
        die(f"找不到裁决文件 {args.verdict_file}")
    verdict_text = read_text(args.verdict_file)
    value, fence_end = _parse_verdict(verdict_text, kind, brief_record.get("brief", ""),
                                      brief_record.get("brief_json"))
    prior = _latest_ledger_record(ledger_records(project), "verdict", args.task, dossier=args.dossier)
    if prior and not getattr(args, "rerule", False):
        die(f"卷宗 {args.dossier} 已有裁决（seq {prior.get('seq')}），重裁决请加 --rerule")
    body = verdict_text[fence_end:]
    rendered = _verdict_render(args.dossier, value, body, brief_record.get("brief", ""),
                               brief_record.get("brief_json"))
    review_path = project.p("reviews", args.dossier + "-verdict.md")
    record = {"type": "verdict", "task": args.task, "dossier": args.dossier,
              "dossier_hash": dossier_hash, "brief_seq": brief_record.get("seq"),
              "brief_record_hash": brief_record.get("record_hash"), "conclusion": value["conclusion"],
              "findings": value["findings"], "responses": value["findings"],
              "verdict": verdict_text, "full": verdict_text, "rendered": rendered,
              "memory": value.get("memory", {})}
    if prior and getattr(args, "rerule", False):
        record["rerule"] = True
        record["replaced_seq"] = prior.get("seq")
    record = append_ledger(project, record)
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    with open(review_path, "w", encoding="utf-8") as stream:
        stream.write(rendered)
    def update(s):
        lg = _dossier_leg(project, args.task, args.dossier, s)
        if lg is not None:
            lg["verdict"] = {"seq": record["seq"], "conclusion": value["conclusion"],
                             "path": review_path, "rerule": bool(getattr(args, "rerule", False))}
        s.pop("revise_counts", None)
    project.update_state(args.task, update)
    memory_written = []
    if not getattr(args, "curation", False):
        from baton_memory.ledger import MemoryLedger, memory_append_approved
        mode = getattr(args, "memory", "as-advised") or "as-advised"
        try:
            entries = _approved_memory_entries(project, args.task, args.dossier, leg,
                                               brief_record.get("brief_json"), mode=mode,
                                               overrides=value.get("memory", {}))
            if entries:
                memory_written = memory_append_approved(MemoryLedger(project), entries,
                                                        source=args.dossier)
            suspended_changed = _apply_suspended_advice(
                project, (brief_record.get("brief_json") or {}).get("memory_advice"), value.get("memory", {}))
        except Exception as exc:
            die("裁决已入账，但记忆写入失败：%s" % exc)
    print(f"已入账裁决 {args.dossier}：{value['conclusion']}（ledger seq {record['seq']}）")
    if memory_written:
        print("已核准入库记忆：" + ", ".join(item.get("id", "?") for item in memory_written))
    if 'suspended_changed' in locals() and suspended_changed:
        print("已处理挂起记忆：" + ", ".join(suspended_changed))
    print(f"裁决文件：{review_path}")
    print(_rule_conclusion_hint(args.task, value["conclusion"], args.dossier, leg))
    revision_key = _revision_key(project, args.dossier) or args.dossier
    count = _revision_count(project, args.task, args.dossier)
    if value["conclusion"] == "REVISE" and count >= 3:
        print(f"REVISE 已累计 {count} 次（事项 {revision_key}），conductor 应停下并把卷宗、简报和裁决交给用户。")


def _decision_request(project, decision_id):
    directory = project.p("decisions")
    candidates = sorted(name for name in os.listdir(directory) if re.match(r"^" + re.escape(decision_id) + r"-.*\.md$", name)) \
        if os.path.isdir(directory) else []
    if not candidates:
        return None
    return os.path.join(directory, candidates[-1])


def _decision_render(decision_id, request_path, decision_text):
    return (f"# 决定 · {decision_id}\n\n请求副本：`{request_path}`\n\n"
            "## 指挥决定\n\n" + (decision_text.strip() or "（无）") + "\n")


def cmd_decide(project, args):
    project.require_init()
    check_task(args.task)
    if not re.fullmatch(r"D\d{3}", args.decision_id or ""):
        die("决策编号必须为 D 加三位数字")
    st = project.load_state(args.task)
    leg = current_leg(st)
    if not leg or leg.get("status") != "decision":
        die("只有以 decision 结束的轮次后面才能 baton decide")
    request_path = _decision_request(project, args.decision_id)
    if not request_path:
        die(f"找不到决策请求 {args.decision_id}-*.md")
    if not os.path.isfile(args.decision_file):
        die(f"找不到决定文件 {args.decision_file}")
    if not verify_ledger(project):
        die("账本校验失败，拒绝入账")
    records = ledger_records(project)
    prior = _latest_ledger_record(records, "decision", args.task, decision_id=args.decision_id)
    if prior and not getattr(args, "rerule", False):
        die(f"决定 {args.decision_id} 已存在（seq {prior.get('seq')}），重做请加 --rerule")
    request_bytes = Path(request_path).read_bytes()
    request_hash = hashlib.sha256(request_bytes).hexdigest()
    decision_text = read_text(args.decision_file)
    if not decision_text.strip():
        die("决定文件不能为空")
    frozen = os.path.join(project.state_dir, "frozen", args.task, "decisions", os.path.basename(request_path))
    os.makedirs(os.path.dirname(frozen), exist_ok=True)
    if not os.path.exists(frozen):
        with open(frozen, "wb") as stream:
            stream.write(request_bytes)
        os.chmod(frozen, 0o444)
    elif Path(frozen).read_bytes() != request_bytes:
        die("项目外请求副本校验失败，拒绝覆盖")
    rendered = _decision_render(args.decision_id, frozen, decision_text)
    review_path = project.p("reviews", args.decision_id + "-decision.md")
    record = {"type": "decision", "task": args.task, "decision_id": args.decision_id,
              "request_path": frozen, "request_sha256": request_hash,
              "decision": decision_text, "full": decision_text, "rendered": rendered}
    if prior and getattr(args, "rerule", False):
        record["rerule"] = True
        record["replaced_seq"] = prior.get("seq")
    record = append_ledger(project, record)
    os.makedirs(os.path.dirname(review_path), exist_ok=True)
    with open(review_path, "w", encoding="utf-8") as stream:
        stream.write(rendered)
    project.update_state(args.task, lambda s: current_leg(s).update(
        decision={"seq": record["seq"], "id": args.decision_id, "path": review_path,
                  "rerule": bool(getattr(args, "rerule", False))}))
    print(f"已入账决定 {args.decision_id}（ledger seq {record['seq']}）")
    print(f"决定文件：{review_path}")


def ledger_dir(project):
    return os.path.join(project.state_dir, "ledger")


def ledger_records(project):
    """Read the append-only ledger without changing it."""
    path = os.path.join(ledger_dir(project), "verdicts.jsonl")
    if not os.path.exists(path):
        return []
    records = []
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                records.append(json.loads(line))
    return records


def _ledger_valid(records, head):
    previous = ""
    for seq, record in enumerate(records, 1):
        if not isinstance(record, dict) or record.get("seq") != seq or record.get("previous_hash", "") != previous:
            return False
        actual = record.get("record_hash")
        payload = dict(record)
        payload.pop("record_hash", None)
        expected = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode()).hexdigest()
        if actual != expected:
            return False
        previous = actual
    return head == previous


def verify_ledger(project):
    directory = ledger_dir(project)
    path = os.path.join(directory, "verdicts.jsonl")
    if not os.path.exists(path):
        return not read_text(os.path.join(directory, "head"), "").strip()
    lock_path = os.path.join(directory, "ledger.lock")
    with _locked(lock_path) as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_SH)
        head = read_text(os.path.join(directory, "head"), "").strip()
        try:
            valid = _ledger_valid(ledger_records(project), head)
        except (OSError, ValueError):
            valid = False
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_UN)
        return valid


def append_ledger(project, record):
    """Append one record to the hash-chained ledger.

    All record types use the same lock, fsync and head update semantics.  The caller
    supplies the payload; seq, previous_hash and record_hash are assigned here.
    """
    directory = ledger_dir(project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "verdicts.jsonl")
    lock_path = os.path.join(directory, "ledger.lock")
    with _locked(lock_path) as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            records = ledger_records(project)
        except (OSError, ValueError) as exc:
            raise ConfigError("账本校验失败") from exc
        if not _ledger_valid(records, read_text(os.path.join(directory, "head"), "").strip()):
            raise ConfigError("账本校验失败")
        previous = records[-1].get("record_hash", "") if records else ""
        record = dict(record)
        record.update({"seq": len(records) + 1, "time": record.get("time") or stamp(),
                       "previous_hash": previous})
        record["record_hash"] = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        with open(os.path.join(directory, "head.tmp"), "w", encoding="utf-8") as stream:
            stream.write(record["record_hash"] + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        platform_support.atomic_replace(os.path.join(directory, "head.tmp"), os.path.join(directory, "head"))
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return record


def append_brief_ledger(project, task, dossier_id, dossier_hash, brief_text, rerun=False, brief_json=None):
    record = {"type": "brief", "task": task, "dossier": dossier_id,
              "dossier_hash": dossier_hash, "brief_sha256": hashlib.sha256(brief_text.encode()).hexdigest(),
              "brief": brief_text}
    if isinstance(brief_json, dict):
        record["brief_json"] = json.loads(json.dumps(brief_json, ensure_ascii=False))
    if rerun:
        record["rerun"] = True
    return append_ledger(project, record)


def diff_tree_entries(store, a, b):
    """[(src_mode, dst_mode, status, top-relative path)] between two commits, under the project.

    Plumbing (diff-tree) so user diff.* settings such as diff.relative or diff.renames
    cannot change the paths we act on.
    """
    raw = store.git("diff-tree", "-r", "--raw", "-z", "--no-renames", a, b, "--", ".", *code_excludes(store))
    parts = raw.split("\0")
    out = []
    i = 0
    while i + 1 < len(parts):
        meta, path = parts[i], parts[i + 1]
        if not meta.startswith(":"):
            i += 1
            continue
        i += 2
        fields = meta[1:].split()
        if len(fields) >= 5:
            out.append((fields[0], fields[1], fields[4][0], path))
    return out


def shadow_excluded_many(store, paths, tracked=None):
    """Return current shadow-excluded paths in one check-ignore subprocess."""
    if store is None or store.kind != "shadow":
        return set()
    tracked = store.user_tracked() if tracked is None else tracked
    candidates = [path for path in paths if path not in tracked]
    if not candidates:
        return set()
    p = subprocess.run([platform_support.executable("git"), "-C", store.top, "check-ignore", "--no-index", "-z", "--stdin"],
                       env=store.env(), input="".join(path + "\0" for path in candidates),
                       capture_output=True, text=True, encoding="utf-8")
    return {path for path in p.stdout.split("\0") if path} if p.returncode == 0 else set()


def shadow_excluded(store, path, tracked=None):
    """Compatibility wrapper for one path; callers handling many paths use the batch helper."""
    return path in shadow_excluded_many(store, [path], tracked)


# ---------------------------------------------------------------- project / state

def git_repo_top(start):
    """Top of the git repository holding start (or holding its nearest existing ancestor)."""
    probe = os.path.abspath(start)
    while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    p = subprocess.run([platform_support.executable("git"), "-C", probe, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, env=clean_git_env(), encoding="utf-8")
    return canonical_dir(p.stdout.strip()) if p.returncode == 0 else None


def canonical_dir(path):
    """Absolute real path with the on-disk letter case (APFS ignores case but keeps it).

    Two spellings of one directory must give one project id; os.path.realpath keeps the
    spelling it was given, so on macOS ask the kernel for the path of an open descriptor.
    """
    path = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.exists(path):
        suffix, ancestor = [], path
        while not os.path.exists(ancestor) and os.path.dirname(ancestor) != ancestor:
            suffix.append(os.path.basename(ancestor))
            ancestor = os.path.dirname(ancestor)
        return os.path.join(canonical_dir(ancestor), *reversed(suffix))
    if sys.platform == "darwin" and fcntl is not None and os.path.isdir(path):
        try:
            fd = os.open(path, os.O_RDONLY)
            try:
                buf = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH
            finally:
                os.close(fd)
            got = buf.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if got and os.path.samefile(got, path):
                return got
        except OSError:
            pass
    return path


def is_initialized(path):
    """`baton init` has run here: .baton/config.json exists (early v1 projects had no baton.json).

    The state root ~/.baton holds only projects/, so it never makes the home directory look
    like a project.
    """
    return os.path.isfile(os.path.join(path, ".baton", "config.json"))


def is_project_dir(path, legacy=True):
    """baton.json marks a project; with legacy=True an initialized .baton/ does too."""
    return os.path.isfile(os.path.join(path, "baton.json")) or (legacy and is_initialized(path))


def nested_project_dirs(root):
    """Nearest nested projects, using the same markers as rollback ownership."""
    found = []
    for directory, names, _ in os.walk(root):
        keep = []
        for name in sorted(names):
            full = os.path.join(directory, name)
            if name in (".git", ".baton", ".council") or os.path.islink(full):
                continue
            if is_project_dir(full):
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
            else:
                keep.append(name)
        names[:] = keep
    return found


_CODE_EXCLUDES_CACHE = {}
_CODE_NESTED_CACHE = {}


def _shadow_prune_dir(name, rel_dir):
    """Fast directory pruning for the shadow's built-in exclude rules."""
    if name.startswith(".venv") or name in {"venv", "node_modules", "__pycache__",
                                              "outputs", "logs", "checkpoints"}:
        return True
    # D001 intentionally excludes only the project's top-level data/ and models/.
    return rel_dir == "." and name in {"data", "models"}


def nested_project_dirs_for_code(root):
    """Nested projects that can affect code statistics, skipping shadow-excluded trees."""
    if root in _CODE_NESTED_CACHE:
        return list(_CODE_NESTED_CACHE[root])
    found = []
    for directory, names, _ in os.walk(root):
        rel_dir = os.path.relpath(directory, root)
        keep = []
        for name in sorted(names):
            full = os.path.join(directory, name)
            if name in (".git", ".baton", ".council") or os.path.islink(full):
                continue
            if _shadow_prune_dir(name, rel_dir):
                continue
            if is_project_dir(full):
                found.append(os.path.relpath(full, root).replace(os.sep, "/"))
            else:
                keep.append(name)
        names[:] = keep
    _CODE_NESTED_CACHE[root] = found
    return list(found)


def code_excludes(store):
    # Quote glob metacharacters so directory names such as "project[1]" stay literal.
    root = store.project.root
    paths = _CODE_EXCLUDES_CACHE.get(root)
    if paths is None:
        nested = nested_project_dirs_for_code(root)
        paths = ["".join("[" + c + "]" if c in "[*?" else c for c in path)
                 for path in nested]
        _CODE_EXCLUDES_CACHE[root] = paths
    return EXCLUDE + [":(exclude,glob)" + path + "/**" for path in paths]


def find_project_dir(start, legacy=True):
    """Find the nearest project dir without crossing the containing git root.

    `init` passes legacy=False: a new project is placed by baton.json only, so an early-v1
    .baton/ higher up cannot capture it without the boundary confirmation.
    """
    start = canonical_dir(start)
    top = git_repo_top(start)
    if not top:
        return start
    cur = start
    while True:
        if is_project_dir(cur, legacy):
            return cur
        if cur == top:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return start


def task_has_v1_refs(st):
    """True when Baton v1 stored checkpoints of this task in the user's repository."""
    if st.get("snapshot_store") == "shadow":
        return False
    return bool(st.get("base_ref") or any(lg.get("start_ref") or lg.get("end_ref") for lg in st.get("legs", [])))


class Project:
    def __init__(self, root):
        self.root = canonical_dir(root)
        self.dir = os.path.join(self.root, ".baton")

    @property
    def project_id(self):
        real = os.path.realpath(self.root)
        digest = hashlib.sha1(os.fsencode(real)).hexdigest()[:8]
        return f"{os.path.basename(real)}-{digest}"

    @property
    def id_file(self):
        return self.p("project-id")

    def recorded_id(self):
        data = read_json(self.id_file)
        return data.get("id") if isinstance(data, dict) else None

    def record_id(self):
        write_json(self.id_file, {"id": self.project_id, "root": self.root})

    def check_identity(self, warn_only=False):
        """Refuse to run on a fresh state dir when the project folder was moved or renamed."""
        old = self.recorded_id()
        if old and old != self.project_id:
            old_dir = os.path.join(os.path.dirname(self.state_dir), old)
            if os.path.isdir(old_dir):
                hint = "移动项目用 baton init --relink；复制项目用 baton init --fresh。"
            else:
                hint = "原状态目录已不存在，移动用 baton init --relink；复制或在新机器上用 baton init --fresh。"
            message = (f"项目目录换过位置或名字：.baton/project-id 记的是 {old}，现在算出来是 {self.project_id}。"
                       f"原来的任务状态和回滚点在 {old_dir}。{hint}"
                       "否则新开的轮次会覆盖 .baton/runs/ 里的旧记录。")
            if warn_only:
                print("警告：" + message, file=sys.stderr)
                return False
            die(message)
        return True

    @property
    def state_dir(self):
        return os.path.join(memory_baton_home(), "projects", self.project_id)

    @property
    def snap_git_dir(self):
        return os.path.join(self.state_dir, "snap.git")

    def store(self, task=None, st=None):
        """SnapStore for a task: the shadow repo, unless Baton v1 already put its refs in the user repo.

        A v1 task without any v1 ref (e.g. one in a non-git project) has nothing to stay
        compatible with, so it moves to the shadow repo with its next checkpoint.
        """
        if task is not None:
            if st is None:
                st = self.read_state(task, {}) or {}
            if task_has_v1_refs(st):
                return SnapStore(self, "repo") if is_git(self.root) else None
        return SnapStore(self, "shadow")

    def config_layers(self):
        """Keep the raw project layers available for v1 migration decisions."""
        project_cfg = read_json(os.path.join(self.dir, "config.json"), {})
        if not isinstance(project_cfg, dict):
            project_cfg = {}
        return project_cfg, self.read_baton_config()

    @property
    def needs_backend_migration(self):
        # A project baton.json without the v2 backend key is an existing v1 file.
        # A brand-new project has no baton.json yet and must inherit the v2 Hermes default.
        return (os.path.exists(self.baton_config_path) and
                not any(isinstance(layer.get("executor"), dict) and "backend" in layer["executor"]
                        for layer in self.config_layers()))

    @property
    def config(self):
        base = read_json(os.path.join(SKILL_DIR, "config.json"), {})
        if os.name == "nt":
            base = deep_merge(base, {"executor": {"backend": "codex"},
                                     "memory": {"backend": "local"}})
        project_cfg, baton_cfg = self.config_layers()
        # quota belongs to the account-level skill configuration.  Keep the
        # project layers from changing it even though all other keys deep-merge.
        project_cfg = {k: v for k, v in project_cfg.items() if k != "quota"}
        baton_cfg = {k: v for k, v in baton_cfg.items() if k != "quota"}
        layers = (project_cfg, baton_cfg)
        mode = base.get("mode", "standard")
        for layer in layers:
            if "mode" in layer:
                mode = layer["mode"]
        defaults = deep_merge(base, lean_overrides()) if mode == "lean" else base
        cfg = deep_merge(deep_merge(defaults, project_cfg), baton_cfg)
        cfg["mode"] = mode
        if mode == "lean" and not any(isinstance(layer.get("executor"), dict)
                                       and "escalation" in layer["executor"]
                                       for layer in layers):
            # Lean is an explicit no-escalation mode.  Keep its compact public
            # shape so callers can inspect the override without inheriting the
            # standard-mode thresholds and model names.
            cfg["executor"]["escalation"] = {"enabled": False}
        project_conductor = any("conductor" in layer for layer in layers)
        project_judge = any("judge" in layer for layer in layers)
        if project_judge:
            legacy_judge = {}
            for layer in layers:
                if isinstance(layer.get("judge"), dict):
                    legacy_judge = deep_merge(legacy_judge, layer["judge"])
            if not project_conductor:
                # v1: judge was the conductor.  The monitor remains at its v2 default.
                cfg["conductor"] = dict(cfg["conductor"], model=legacy_judge.get("model"))
                if "council_for_major_decisions" in legacy_judge:
                    cfg["conductor"]["council_for_major_decisions"] = legacy_judge["council_for_major_decisions"]
            else:
                # Early v2 files used judge for the read-only monitor.  Merge it as an alias,
                # while an explicit monitor in the same layer takes precedence.
                monitor = dict(defaults.get("monitor") or {})
                for layer in layers:
                    if isinstance(layer.get("judge"), dict):
                        monitor = deep_merge(monitor, layer["judge"])
                    if isinstance(layer.get("monitor"), dict):
                        monitor = deep_merge(monitor, layer["monitor"])
                cfg["monitor"] = monitor
        cfg.pop("judge", None)
        validate_config(cfg)
        if self.needs_backend_migration:
            cfg["executor"]["backend"] = "codex"
        cfg["project"] = {"root": self.root, "id": self.project_id, "state_dir": self.state_dir}
        baton_home = memory_baton_home(cfg)
        memory_python = platform_support.venv_python(os.path.abspath(os.path.expanduser(baton_home)))
        if os.path.isfile(memory_python) and "baton-memory" not in cfg["executor"].get("toolsets", []):
            cfg["executor"]["toolsets"] = list(cfg["executor"].get("toolsets", [])) + ["baton-memory"]
        return validate_config(cfg)

    @property
    def judge_renamed_to_monitor(self):
        project_cfg, baton_cfg = self.config_layers()
        return (any("conductor" in layer for layer in (project_cfg, baton_cfg)) and
                any("judge" in layer for layer in (project_cfg, baton_cfg)))

    @property
    def baton_config_path(self):
        return os.path.join(self.root, "baton.json")

    def read_baton_config(self):
        path = self.baton_config_path
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{path} 不是合法 JSON：第 {e.lineno} 行第 {e.colno} 列：{e.msg}") from e
        except OSError as e:
            raise ConfigError(f"无法读取 {path}：{e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{path} 顶层必须是 JSON object，实际是 {type(data).__name__}")
        return data

    def config_paths(self):
        return [
            (os.path.join(SKILL_DIR, "config.json"), True),
            (os.path.join(self.dir, "config.json"), os.path.exists(os.path.join(self.dir, "config.json"))),
            (self.baton_config_path, os.path.exists(self.baton_config_path)),
        ]

    def p(self, *parts):
        return os.path.join(self.dir, *parts)

    def task_dir(self, task):
        return self.p("tasks", task)

    def state_task_dir(self, task):
        return os.path.join(self.state_dir, "tasks", task)

    def runs_dir(self, task):
        return self.p("runs", task)

    def leg_path(self, task, n, suffix):
        return os.path.join(self.runs_dir(task), f"leg-{n:03d}.{suffix}")

    def state_path(self, task):
        return os.path.join(self.state_task_dir(task), "state.json")

    def state_lock_path(self, task):
        return os.path.join(self.state_task_dir(task), "state.lock")

    def launch_lock_path(self):
        return os.path.join(self.state_dir, "launch.lock")

    def legacy_state_path(self, task):
        return os.path.join(self.task_dir(task), "state.json")

    def import_legacy_state(self, task):
        """Import a v1 task state once, under the state lock; project files stay where they are.

        While a Baton v1 runner is still running the task's leg, nothing is imported: that
        runner keeps writing the old file, so a copy would split the state.  The v1 state is
        returned instead, for read-only use.
        """
        target = self.state_path(task)
        if os.path.exists(target):
            return None
        legacy = self.legacy_state_path(task)
        old = read_json(legacy) if os.path.isfile(legacy) else None
        if not isinstance(old, dict):
            return None
        leg = old["legs"][-1] if old.get("legs") else None
        if self.legacy_state_in_flight(task, old=old, leg=leg):
            return old
        os.makedirs(self.state_task_dir(task), exist_ok=True)
        with _locked(self.state_lock_path(task)):
            if not os.path.exists(target):
                old["imported_from_v1"] = stamp()
                write_json(target, old)
        return None

    def legacy_state_in_flight(self, task, old=None, leg=None):
        """Whether a v1 runner may still be writing the project-local state file."""
        if os.path.exists(self.state_path(task)):
            return False
        if old is None:
            legacy = self.legacy_state_path(task)
            old = read_json(legacy) if os.path.isfile(legacy) else None
        if not isinstance(old, dict):
            return False
        if leg is None:
            leg = old["legs"][-1] if old.get("legs") else None
        if not leg or leg.get("status") != "running":
            return False
        pid = leg.get("runner_pid")
        if pid:
            marker = runner_marker(task, leg.get("n"))
            if pid_alive(pid, marker):
                return True
            # A live PID without the marker may be a reused PID.  Keep the conservative
            # v1 protection for one day, then allow the stale state to be imported.
            started = leg.get("started_at")
            try:
                recent = started is not None and now() - float(started) <= 24 * 60 * 60
            except (TypeError, ValueError):
                recent = False
            return recent and pid_alive(pid)
        started = leg.get("started_at")
        try:
            return started is not None and now() - float(started) < 30
        except (TypeError, ValueError):
            return False

    def task_exists(self, task):
        return (os.path.isfile(os.path.join(self.task_dir(task), "brief.md"))
                or os.path.exists(self.state_path(task))
                or os.path.exists(self.legacy_state_path(task)))

    def read_state(self, task, default=None):
        in_flight = self.import_legacy_state(task)
        if in_flight is not None:
            return in_flight
        return read_json(self.state_path(task), default)

    def require_init(self):
        if not is_initialized(self.root):
            die(f"{self.root} 还没有初始化，先运行 baton init")

    def load_state(self, task):
        st = self.read_state(task)
        if st is None:
            die(f"任务 {task} 不存在（{self.state_path(task)}）")
        return st

    def update_state(self, task, fn):
        """Read-modify-write the task state under a file lock."""
        if self.import_legacy_state(task) is not None:
            die(f"任务 {task} 还有 Baton v1 的一轮在运行，它还在写旧的状态文件；"
                "等这一轮结束再用新版本操作这个任务")
        os.makedirs(self.state_task_dir(task), exist_ok=True)
        with (platform_support.exclusive_lock(self.state_lock_path(task)) if os.name == "nt"
              else open(self.state_lock_path(task), "w", encoding="utf-8")) as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            st = read_json(self.state_path(task), {})
            fn(st)
            write_json(self.state_path(task), st)
            return st

    def tasks(self):
        found = set()
        for d in (self.p("tasks"), os.path.join(self.state_dir, "tasks")):
            if os.path.isdir(d):
                found.update(name for name in os.listdir(d)
                             if os.path.isdir(os.path.join(d, name)) and TASK_RE.fullmatch(name))
        return sorted(found)


def current_leg(st):
    return st["legs"][-1] if st.get("legs") else None


def check_task(task):
    if not TASK_RE.fullmatch(task or ""):
        die("任务名只能用字母、数字、下划线和连字符，以字母或数字开头，不超过 64 个字符")


def runner_marker(task, n):
    return f"_run {task} {n}"


def codex_alive(leg):
    """The leg's own codex process is running (identified by its unique -o path, not just 'codex')."""
    if not leg or leg.get("codex_exited"):
        return False
    if leg.get("backend") == "hermes":
        profile = leg.get("hermes_profile", "baton-exec-v2")
        return pid_alive(leg.get("codex_pid"), f"-p {profile}") or pid_alive(leg.get("codex_pid"))
    return pid_alive(leg.get("codex_pid"), leg.get("last") or "\0")


def kill_codex(leg):
    """Stop the leg's codex process group (codex is started as a session leader). True if it was alive."""
    if not codex_alive(leg):
        return False
    pid = leg["codex_pid"]
    if os.name == "nt":
        platform_support.stop_tree(pid, (8, 3))
        return True
    for sig, grace in ((signal.SIGTERM, 8), (signal.SIGKILL, 3)):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        deadline = now() + grace
        while now() < deadline:
            if not pid_alive(pid):
                return True
            time.sleep(0.2)
    return True


def refresh_runner_liveness(project, task, st):
    """A leg marked running whose runner is gone died without cleanup: stop its codex, mark failed."""
    if project.legacy_state_in_flight(task):
        return st
    leg = current_leg(st)
    if not leg:
        curation = st.get("curation_monitor") or {}
        if curation.get("status") == "running" and curation.get("pid") \
                and not pid_alive(curation.get("pid"), curation.get("marker")):
            return project.update_state(task, lambda s: s.update(curation_monitor=dict(
                curation, status="failed", pid=None, ended_at=now(), error="curation monitor 进程意外退出")))
        return st
    monitor = leg.get("monitor") or {}
    check = leg.get("monitor_check") or {}
    curation = st.get("curation_monitor") or {}
    if curation.get("status") == "running":
        curation_pid = curation.get("pid")
        if curation_pid and not pid_alive(curation_pid, curation.get("marker")):
            def curation_failed(s):
                current = dict(s.get("curation_monitor") or {})
                if current.get("status") == "running":
                    current.update(status="failed", pid=None, ended_at=now(), error="curation monitor 进程意外退出")
                    s["curation_monitor"] = current
            st = project.update_state(task, curation_failed)
        else:
            return st
    if check.get("status") == "running":
        check_pid = check.get("pid")
        marker = check.get("marker") or f"_monitor_check {task} {check.get('id', '')}"
        if check_pid and not pid_alive(check_pid, marker):
            try:
                young = now() - float(check.get("started_at", now())) < STARTUP_GRACE_SECONDS
            except (TypeError, ValueError):
                young = False
            if not (young and pid_alive(check_pid)):
                _check_failure_update(project, task, leg.get("n"), check.get("id"), "轮中检查进程意外退出")
                st = project.load_state(task)
                leg = current_leg(st)
                monitor = leg.get("monitor") or {}
    if monitor.get("status") == "running":
        monitor_pid = monitor.get("pid")
        marker = monitor.get("marker")
        if monitor_pid and not pid_alive(monitor_pid, marker):
            # The detached monitor process may still be between fork and exec; give it
            # the same marker-registration grace window as the executor runner.
            try:
                monitor_young = now() - float(monitor.get("started_at", now())) < STARTUP_GRACE_SECONDS
            except (TypeError, ValueError):
                monitor_young = False
            if monitor_young and pid_alive(monitor_pid):
                return st
            def monitor_failed(s):
                current = current_leg(s)
                if current and (current.get("monitor") or {}).get("status") == "running":
                    current["monitor"].update(status="failed", pid=None, ended_at=now(),
                                               error="runner 退出时 monitor 未完成")
            return project.update_state(task, monitor_failed)
        return st
    if leg.get("status") != "running":
        return st
    if not leg.get("runner_pid") and now() - leg["started_at"] < STARTUP_GRACE_SECONDS:
        return st  # launch_leg has not recorded the runner pid yet
    if pid_alive(leg.get("runner_pid"), runner_marker(task, leg["n"])):
        return st
    # Popen records the pid before the detached child has completed exec.  During
    # that short window its command line does not contain the marker yet.
    if leg.get("runner_pid") and now() - leg["started_at"] < STARTUP_GRACE_SECONDS \
            and pid_alive(leg.get("runner_pid")):
        return st
    # A runner writes its result file just before the final state update.  A conductor
    # that calls wait in that narrow window must not turn a successful leg into FAILED.
    result = normalize_executor_result(read_json(leg.get("last", ""))) if leg.get("last") else None
    if isinstance(result, dict) and result.get("status") in ("done", "decision", "report", "blocked"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            latest = project.read_state(task, {}) or {}
            latest_leg = current_leg(latest)
            if not latest_leg or latest_leg.get("n") != leg["n"] or latest_leg.get("status") != "running":
                return latest
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

        def finish_result(s):
            lg = current_leg(s)
            if lg and lg.get("n") == leg["n"] and lg.get("status") == "running":
                lg.update(status=result["status"], result=result, ended_at=now(), codex_exited=True,
                          error="runner 写完结果后退出，未做轮末核对")
                s["status"] = "done" if result["status"] == "done" else "idle"
        return project.update_state(task, finish_result)
    killed = kill_codex(leg)

    def mark(s):
        lg = current_leg(s)
        if lg.get("status") == "running":
            lg["status"] = "failed"
            lg["error"] = "runner 进程意外退出（机器重启或被杀）" + ("；已终止残留的 codex 进程" if killed else "")
            lg["ended_at"] = now()
            lg["codex_exited"] = True
            s["status"] = "idle"
    return project.update_state(task, mark)


def mark_reported(project, task):
    def mark(s):
        if current_leg(s):
            current_leg(s)["reported"] = True
    project.update_state(task, mark)


def surface_unreported(project, task, st, action):
    """If the last leg ended on its own and nobody has seen its event yet, show it and stop."""
    leg = current_leg(st)
    if leg and leg["status"] not in ("running", "stopped") and not leg.get("reported"):
        print(leg_event_text(project, task, leg))
        mark_reported(project, task)
        die(f"第 {leg['n']} 轮在{action}前已经自己结束（{leg['status']}），上面的事件还没处理过。"
            f"先按事件处理；处理完再执行同一条命令即可。", 4)


# ---------------------------------------------------------------- events digest

def read_events(path, offset=0):
    """Complete JSONL events after byte `offset`; returns (events, new_offset)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], offset
    events = []
    for line in data[:cut + 1].splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events, offset + cut + 1


def thread_id_from(path):
    events, _ = read_events(path)
    for ev in events:
        if ev.get("type") == "thread.started" and ev.get("thread_id"):
            return ev["thread_id"]
    return None


def digest(events):
    cmds, files, msgs, errors, running = [], {}, [], [], {}
    usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for ev in events:
        et = ev.get("type", "")
        item = ev.get("item") if isinstance(ev.get("item"), dict) else {}
        it = item.get("type", "")
        if et == "item.started" and it == "command_execution":
            running[item.get("id")] = item.get("command", "")
        elif et == "item.completed":
            if it == "command_execution":
                running.pop(item.get("id"), None)
                cmds.append((item.get("command", ""), item.get("exit_code")))
            elif it == "file_change":
                for ch in item.get("changes") or []:
                    if isinstance(ch, dict):
                        files[ch.get("path", "?")] = ch.get("kind", "?")
            elif it == "agent_message":
                msgs.append(item.get("text", ""))
            elif it == "error":
                errors.append(item.get("message", json.dumps(item, ensure_ascii=False)))
        elif et in ("turn.failed", "error"):
            err = ev.get("error") or ev
            errors.append(err.get("message") if isinstance(err, dict) else str(err))
        elif et == "turn.completed":
            u = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
            for k in usage:
                try:
                    usage[k] += int(u.get(k) or 0)
                except (TypeError, ValueError):
                    pass
    return {"commands": cmds, "files": files, "messages": msgs, "errors": errors,
            "running": list(running.values()), "usage": usage}


def _event_time(event, fallback=None):
    """Return an event timestamp without depending on one Codex event schema version."""
    value = event.get("timestamp") or event.get("created_at") or event.get("at")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback if fallback is not None else now()


def leg_terminal_calls(project, task, leg, since=None):
    """Read Codex command executions from a leg event stream.

    This is intentionally a backend seam: a future Hermes adapter can provide the same
    list without changing the supervision rules or dossier evidence format.
    """
    backend = leg.get("backend")
    if backend is None:
        try:
            backend = config_for_state(project, project.read_state(task, {}) or {})["executor"].get("backend")
        except (ConfigError, KeyError, TypeError):
            backend = "codex"
    if backend == "hermes":
        return hermes_terminal_calls(project, task, leg, since=since)
    events, _ = read_events(leg.get("events", ""))
    calls = []
    for index, event in enumerate(events):
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") != "command_execution":
            continue
        if event.get("type") != "item.completed":
            continue
        at = _event_time(event, _event_time(item, float(leg.get("started_at", now())) + (index + 1) * 1e-6))
        if since is not None and at <= float(since):
            continue
        raw = str(item.get("command", "") or "")
        calls.append({"command": _decode_shell_command(raw), "raw": raw,
                      "exit_code": item.get("exit_code"), "at": at,
                      "time": stamp(at, fmt="%Y-%m-%d %H:%M:%S")})
    return calls


def _decode_shell_command(command):
    """Unwrap Codex's `/bin/zsh -lc <command>` event envelope.

    The raw event remains available for audit while all rule and external-action
    consumers compare the command the executor actually asked the shell to run.
    """
    value = str(command or "").strip()
    try:
        parts = shlex.split(value)
    except ValueError:
        return value
    if len(parts) >= 3 and os.path.basename(parts[0]) in {"zsh", "bash", "sh"}:
        if parts[1] in {"-c", "-lc", "-cl"}:
            return parts[2]
    return value


def _strip_leading_cd(command):
    """Remove only the permitted ``cd <dir> &&`` prefix from a shell command."""
    value = str(command or "")
    match = re.match(r"^\s*cd\s+(?:'[^']*'|\"[^\"]*\"|[^&\n]+?)\s*&&\s*(.*)$", value, re.S)
    return match.group(1) if match else value.strip()


def verify_memory_command(project, task, leg, candidate_text):
    """Verify an executor command against this leg's successful terminal calls."""
    expected = str(candidate_text or "")
    if not expected:
        return False
    try:
        cfg = config_for_state(project, project.load_state(task))
        verification = leg.get("verify") or {}
        configured = (cfg.get("supervision") or {}).get("test_command", "")
        if expected == configured and not verification.get("skipped") and verification.get("exit_code") == 0:
            return True
    except Exception:
        pass
    calls = leg_terminal_calls(project, task, leg, since=leg.get("started_at"))
    for call in calls:
        command = str(call.get("command") or "")
        stripped = _strip_leading_cd(command)
        # A compound command can hide an unobserved side effect.  The one
        # allowed ``&&`` is consumed by _strip_leading_cd above.
        if re.search(r"[|;&<>]", stripped):
            continue
        if stripped == expected and call.get("exit_code") == 0:
            return True
    return False


def _memory_candidates_for_rule(project, task, leg):
    dossier_id = (leg.get("dossier") or {}).get("id") or (leg.get("monitor") or {}).get("id")
    if not dossier_id:
        raise ConfigError("记忆核准缺少绑定卷宗")
    good, error = verify_dossier(project, dossier_id)
    if not good:
        raise ConfigError("核准前卷宗校验失败：" + error)
    report = read_text(os.path.join(dossier_path(project, dossier_id), "report.html"))
    st = project.load_state(task)
    return _dossier_memory_candidates(project, task, st, leg, report)


def _approved_memory_entries(project, task, dossier_id, leg, brief_json, mode="as-advised", overrides=None):
    """Turn monitor advice plus conductor overrides into ledger entries."""
    if mode != "as-advised":
        return []
    candidates = _memory_candidates_for_rule(project, task, leg)
    by_id = {item["id"]: item for item in candidates}
    advice = (brief_json or {}).get("memory_advice") if isinstance(brief_json, dict) else []
    advice = advice if isinstance(advice, list) else []
    overrides = overrides if isinstance(overrides, dict) else {}
    new_no = 0
    selected = []
    for item in advice:
        if not isinstance(item, dict):
            continue
        ident = str(item.get("id", ""))
        if ident.startswith("M"):
            continue
        if ident == "new":
            new_no += 1
            key = "new-%d" % new_no
            candidate = {"id": key, "kind": item.get("kind"), "text": item.get("text"),
                         "source": item.get("source", "monitor"), "anchors": item.get("anchors") or []}
        else:
            key = ident
            candidate = dict(by_id.get(ident) or {})
            if not candidate:
                continue
        action = item.get("action")
        choice = overrides.get(key, overrides.get(ident))
        if choice == "reject":
            action = "reject"
        elif choice == "approve":
            action = "approve"
        elif isinstance(choice, dict) and isinstance(choice.get("text"), str) and choice["text"].strip():
            action = "rewrite"
            candidate["text"] = choice["text"]
            candidate["pinned"] = bool(choice.get("pinned", False))
        if action in {"reject"}:
            continue
        if action not in {"approve", "rewrite", "new"}:
            continue
        if action == "rewrite" and isinstance(item.get("text"), str) and item["text"].strip():
            candidate["text"] = item["text"]
        actor = str(candidate.get("source", "executor"))
        verified = True
        verification = "verified"
        if candidate.get("kind") in {"command", "convention"} and "executor" in actor.lower():
            verified = verify_memory_command(project, task, leg, candidate.get("text"))
            verification = "verified" if verified else "claimed"
            if candidate.get("kind") == "convention" and not verified:
                # An executor's untested convention cannot become a rule.
                continue
        selected.append({"kind": candidate.get("kind"), "text": candidate.get("text"),
                         "source": dossier_id, "source_actor": actor, "origin": actor,
                         "anchors": candidate.get("anchors") or [dossier_id],
                         "task": task, "leg": leg.get("n"), "end_snapshot": leg.get("end_ref", ""),
                         "candidate_id": key, "verified": verified,
                         "verification": verification, "verification_status": verification,
                         "claimed": not verified, "pinned": bool(candidate.get("pinned", False))})
    return selected


def _apply_suspended_advice(project, advice, overrides=None):
    from baton_memory.ledger import MemoryLedger
    ledger = MemoryLedger(project)
    latest = ledger.latest()
    overrides = overrides if isinstance(overrides, dict) else {}
    changed = []
    for item in advice if isinstance(advice, list) else []:
        if not isinstance(item, dict) or not str(item.get("id", "")).startswith("M"):
            continue
        ident = item["id"]
        if ident not in latest or latest[ident].get("status") != "suspended":
            continue
        action = item.get("action")
        choice = overrides.get(ident)
        if choice == "reject":
            action = "retire"
        elif choice == "approve":
            action = "restore"
        if action == "restore":
            ledger.append_status(ident, "active", status_reason="conductor 核准恢复")
            changed.append(ident + ":restore")
        elif action == "retire":
            ledger.append_status(ident, "retired", status_reason="conductor 核准退役")
            changed.append(ident + ":retire")
    return changed


def _seed_candidates(project):
    from baton_memory.candidates import number_candidates
    paths = [project.p("log.md")]
    for directory in (project.p("reviews"), project.p("decisions")):
        if os.path.isdir(directory):
            paths.extend(os.path.join(directory, name) for name in sorted(os.listdir(directory))
                         if name.endswith((".md", ".html")))
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = os.path.join(project.root, name)
        if os.path.isfile(path):
            paths.append(path)
    candidates = []
    for path in paths:
        if not os.path.isfile(path):
            continue
        text = read_text(path)
        if not text.strip():
            continue
        rel = os.path.relpath(path, project.root).replace(os.sep, "/")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        # Seed one bounded, source-preserving fact per source file.  The monitor
        # can split or reject it in the first curation-style ruling.
        kind = "convention" if os.path.basename(path) in {"AGENTS.md", "CLAUDE.md"} else (
            "precedent" if "/decisions/" in "/" + rel or "/reviews/" in "/" + rel else "event")
        body = "\n".join(lines[:20])[:1800]
        candidates.append({"kind": kind, "text": body, "source": "user" if kind == "convention" else "conductor",
                           "anchors": [rel]})
    return number_candidates(candidates)


def build_seed_dossier(project, task):
    st = project.load_state(task)
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg):
        return None
    candidates = _seed_candidates(project)
    dossier_id = task + "-seed"
    root = dossier_path(project, dossier_id)
    if os.path.isdir(root):
        _unseal_dossier(root)
        shutil.rmtree(root)
    os.makedirs(root, exist_ok=True)
    report_path = project.p("tasks", task, "seed-report.html")
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    _dossier_write(report_path, "<h2>记忆候选</h2><pre data-memory-candidates=\"true\">" +
                   json.dumps(candidates, ensure_ascii=False) + "</pre>\n")
    prompt = fill(read_text(os.path.join(TEMPLATES, "monitor-prompt.md")),
                  {"TASK": task, "DOSSIER": dossier_id, "DOSSIER_TYPE": "seed"})
    _dossier_write(os.path.join(root, "MONITOR_PROMPT.md"), prompt)
    _dossier_write(os.path.join(root, "charter.md"), _charter_text(project, task, st))
    _dossier_write(os.path.join(root, "report.html"), read_text(report_path))
    _dossier_write(os.path.join(root, "evidence.md"), "# Seed 来源\n\n从 log.md、reviews、decisions 和 AGENTS/CLAUDE.md 生成。\n")
    _dossier_write(os.path.join(root, "diff.patch"), "")
    _dossier_write(os.path.join(root, "precedents.md"), _precedents_text(project, task))
    _dossier_write(os.path.join(root, "memory.md"), _memory_dossier_text(project, task, st,
                   current_leg(st) or {"memory_core": ""}, candidates))
    files = _dossier_files(root)
    write_json(os.path.join(root, "manifest.json"), {"dossier": dossier_id, "task": task, "type": "seed", "files": files,
        "total_sha256": _dossier_total(files), "created_at": now()})
    _seal_dossier(root)
    project.update_state(task, lambda s: (current_leg(s) or {}).update(
        dossier={"id": dossier_id, "type": "seed"}, monitor={"id": dossier_id, "status": "running"}))
    return dossier_id


_HERMES_DB_READER = r'''
import json, sqlite3, sys
db_path, root_session, since = sys.argv[1], sys.argv[2], float(sys.argv[3]) if sys.argv[3] else None
out = []
try:
    conn = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    sessions = {}
    for row in conn.execute("select id, parent_session_id from sessions"):
        sessions[row["id"]] = row["parent_session_id"]
    ids = [root_session] if root_session else []
    i = 0
    while i < len(ids):
        parent = ids[i]; i += 1
        ids.extend(sid for sid, parent_id in sessions.items() if parent_id == parent and sid not in ids)
    placeholders = ",".join("?" for _ in ids)
    if not placeholders:
        raise RuntimeError("missing session")
    columns = {row[1] for row in conn.execute("pragma table_info(messages)")}
    selected = [name for name in ("id", "session_id", "role", "content", "tool_calls", "tool_name", "tool_call_id", "timestamp") if name in columns]
    rows = conn.execute("select " + ",".join(selected) + " from messages where session_id in (" + placeholders + ") order by timestamp, id", ids).fetchall()
    pending = {}
    for row in rows:
        item = dict(row)
        ts = float(item.get("timestamp") or 0)
        if since is not None and ts <= since:
            continue
        if item.get("role") == "assistant" and item.get("tool_calls"):
            try: calls = json.loads(item["tool_calls"])
            except Exception: calls = []
            if isinstance(calls, list):
                for call in calls:
                    fn = call.get("function") if isinstance(call, dict) else {}
                    name = fn.get("name") if isinstance(fn, dict) else ""
                    if name in ("terminal", "run_command", "shell"):
                        try: args = json.loads(fn.get("arguments") or "{}")
                        except Exception: args = {}
                        call_id = call.get("id") or call.get("call_id") or ""
                        pending[call_id] = {"command": args.get("command") or args.get("cmd") or "", "at": ts}
        if item.get("tool_name") in ("terminal", "run_command", "shell"):
            content = item.get("content") or ""
            try: payload = json.loads(content)
            except Exception: payload = {}
            call = pending.get(item.get("tool_call_id"), {})
            command = call.get("command") or payload.get("command") or payload.get("cmd") or ""
            exit_code = payload.get("exit_code") if isinstance(payload, dict) else None
            out.append({"command": command, "exit_code": exit_code, "at": ts, "time": ts})
    conn.close()
except Exception as exc:
    print(json.dumps({"warning": str(exc)}))
else:
    print(json.dumps(out))
'''


def hermes_terminal_calls(project, task, leg, since=None):
    """Read terminal calls from the profile state.db through Hermes' venv Python."""
    cfg = config_for_state(project, project.read_state(task, {}) or {})
    db_path = os.path.join(hermes_profile_dir(cfg), "state.db")
    session_id = leg.get("thread_id") or (project.read_state(task, {}) or {}).get("thread_id")
    python = hermes_python(cfg)
    if not python or not session_id or not os.path.isfile(db_path):
        return []
    try:
        result = subprocess.run([python, "-c", _HERMES_DB_READER, db_path, str(session_id),
                                 "" if since is None else str(since)],
                                env=_hermes_subprocess_env(cfg), capture_output=True, text=True, timeout=10, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"WARN：Hermes state.db 读取失败：{exc}")
        return []
    value = None
    for line in reversed((result.stdout or "").splitlines()):
        try:
            value = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(value, list):
        if value and isinstance(value, dict) and value.get("warning"):
            print("WARN：Hermes state.db 读取失败：" + str(value["warning"]))
        return []
    for call in value:
        if isinstance(call, dict) and isinstance(call.get("at"), (int, float)):
            call.setdefault("raw", call.get("command", ""))
            call["time"] = stamp(call["at"], fmt="%Y-%m-%d %H:%M:%S")
    return value


def external_action_findings(before_refs, after_refs, calls):
    """Return auditable external-action records for either executor backend."""
    actions = []
    if before_refs != after_refs:
        actions.append({"type": "remote_refs_changed", "before": before_refs, "after": after_refs})
    pattern = re.compile(r"(?:^|\s)(?:git\s+push|ssh(?:\s|$)|scp(?:\s|$)|rsync(?:\s|$))", re.I)
    for call in calls or []:
        command = str(call.get("command", ""))
        if pattern.search(command):
            actions.append({"type": "external_command", "command": command,
                            "exit_code": call.get("exit_code"), "at": call.get("at")})
    return actions


def git_remote_refs(root):
    result = subprocess.run([platform_support.executable("git"), "-C", root, "for-each-ref", "--format=%(refname) %(objectname)",
                             "refs/remotes"], env=clean_git_env(), capture_output=True, text=True, encoding="utf-8")
    return result.stdout.splitlines() if result.returncode == 0 else []


def record_external_actions(project, task, leg_n, before_refs, after_refs, calls):
    actions = external_action_findings(before_refs, after_refs, calls)
    if not actions:
        return []
    def update(state):
        leg = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if leg is not None:
            existing = leg.setdefault("external_actions", [])
            for action in actions:
                if action not in existing:
                    existing.append(action)
    project.update_state(task, update)
    event_path = project.leg_path(task, leg_n, "events.jsonl")
    try:
        with open(event_path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "baton.external_action", "actions": actions,
                                     "timestamp": now()}, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return actions


def _probe_blob(store, ref, path):
    if not ref:
        return ""
    return store.git("show", f"{ref}:{path}", check=False)


def _probe_added_lines(store, old_ref, new_ref, path):
    text = store.git("diff", "--unified=0", old_ref, new_ref, "--", path, check=False)
    return [line[1:] for line in text.splitlines() if line.startswith("+") and not line.startswith("+++")]


def _probe_changed_stats(store, old_ref, new_ref, path):
    text = store.git("diff", "--numstat", old_ref, new_ref, "--", path, check=False).strip()
    if not text:
        return 0, 0
    fields = text.split("\t", 2)
    if len(fields) < 2:
        return 0, 0
    try:
        return int(fields[0]) if fields[0].isdigit() else 0, int(fields[1]) if fields[1].isdigit() else 0
    except ValueError:
        return 0, 0


def _is_test_file(path):
    """Whether a Python path is a test file for destructive-rule accounting."""
    normalized = path.replace(os.sep, "/")
    if not normalized.endswith(".py"):
        return False
    parts = normalized.split("/")
    name = parts[-1]
    return (name.startswith("test_") or name.endswith("_test.py")
            or len(parts) > 1 and parts[-2] in {"test", "tests"})


def _new_report_file(project, started_at):
    reports = project.p("reports")
    if not os.path.isdir(reports):
        return False
    for name in os.listdir(reports):
        path = os.path.join(reports, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) >= float(started_at):
                return True
        except OSError:
            continue
    return False
def _rule_alert(leg, rule, category, evidence, key=None):
    key = key or rule
    existing = leg.setdefault("alert_keys", [])
    if key in existing:
        return None
    existing.append(key)
    alerts = leg.setdefault("alerts", [])
    alert = {"id": f"A{len(alerts) + 1}", "rule": rule, "category": category,
             "evidence": str(evidence), "at": now()}
    alerts.append(alert)
    return alert


def _record_rule_state(project, task, leg_n, changes):
    def update(state):
        current = next((item for item in state.get("legs", []) if item.get("n") == leg_n), None)
        if current is None:
            return
        for key, value in changes.items():
            if isinstance(value, dict) and isinstance(current.get(key), dict):
                current[key].update(value)
            else:
                current[key] = value
    return project.update_state(task, update)


def run_rule_checks(project, task, st, leg):
    """Run cheap incremental supervision rules against a probe snapshot."""
    probe_started = time.monotonic()
    cfg = config_for_state(project, st)
    if cfg.get("mode") == "lean" or not monitor_enabled(cfg):
        return []
    store = project.store(task, st)
    if store is None:
        return []
    try:
        probe_commit = store.snapshot(f"baton probe {task}/{leg['n']} {stamp()}")
        probe_ref = f"refs/baton/{task}/probe"
        store.git("update-ref", probe_ref, probe_commit)
    except Exception:
        return []
    old_ref = leg.get("probe_commit") or leg.get("start_ref")
    if not old_ref:
        old_ref = probe_commit
    entries = diff_tree_entries(store, old_ref, probe_commit) if old_ref != probe_commit else []
    alerts = []
    alert_keys = set(leg.get("alert_keys") or [])
    updated_hashes = dict(leg.get("probe_hashes") or {})
    changed_at = now() if entries else float(leg.get("last_file_change_at") or leg.get("started_at") or now())
    alert_cfg = (cfg.get("supervision") or {}).get("alerts") or {}
    for src_mode, dst_mode, status, path in entries:
        old_text = _probe_blob(store, old_ref, path) if status != "A" else ""
        new_text = _probe_blob(store, probe_commit, path) if status != "D" else ""
        old_hash = hashlib.sha256(old_text.encode()).hexdigest() if old_text else None
        new_hash = hashlib.sha256(new_text.encode()).hexdigest() if new_text else None
        if new_hash:
            prior = updated_hashes.setdefault(path, [])
            if old_hash and not prior:
                prior.append(old_hash)
            if new_hash in prior and f"hash_cycle:{path}" not in alert_keys:
                alert = _rule_alert(leg, "content_hash_cycle", "stall", path + " 内容哈希回到旧值", f"content_hash_cycle:{path}")
                if alert:
                    alerts.append(alert)
            if new_hash not in prior:
                prior.append(new_hash)
                if len(prior) > 20:
                    del prior[:-20]
        if status == "D" and _is_test_file(path):
            alert = _rule_alert(leg, "deleted_test_file", "breaking", path, f"deleted_test_file:{path}")
            if alert:
                alerts.append(alert)
        if _is_test_file(path) and status != "D":
            old_tests = len(re.findall(r"^\s*def\s+test_", old_text, re.M)) + len(re.findall(r"\bassert\b", old_text))
            new_tests = len(re.findall(r"^\s*def\s+test_", new_text, re.M)) + len(re.findall(r"\bassert\b", new_text))
            if old_tests > new_tests:
                alert = _rule_alert(leg, "test_count_decreased", "breaking",
                                    f"{path}: {old_tests} -> {new_tests}", f"test_count_decreased:{path}")
                if alert:
                    alerts.append(alert)
        added = _probe_added_lines(store, old_ref, probe_commit, path) if status != "D" else []
        added_text = "\n".join(added)
        skip_match = None
        comment_test = None
        if _is_test_file(path):
            skip_match = re.search(
                r"pytest\.mark\.(?:skip|skipif)|pytest\.skip\s*\(|pytest\.mark\.xfail|"
                r"pytest\.xfail\s*\(|@unittest\.skip|unittest\.skip\s*\(|self\.skipTest\s*\(",
                added_text)
            comment_test = re.search(r"^\s*#.*\bdef\s+test_", added_text, re.M)
        if skip_match or comment_test:
            alert = _rule_alert(leg, "test_disabled", "breaking", f"{path}: {skip_match.group(0) if skip_match else 'commented test'}",
                                f"test_disabled:{path}")
            if alert:
                alerts.append(alert)
        if re.search(r"except\s*(?:Exception)?\s*:\s*\n\s*pass\b", new_text) and re.search(r"except\s*(?:Exception)?\s*:", added_text):
            alert = _rule_alert(leg, "swallowed_exception", "breaking", path, f"swallowed_exception:{path}")
            if alert:
                alerts.append(alert)
        added_lines, deleted_lines = _probe_changed_stats(store, old_ref, probe_commit, path)
        ratio = float(alert_cfg.get("delete_ratio", 3) or 3)
        if deleted_lines >= 20 and deleted_lines > added_lines * ratio:
            alert = _rule_alert(leg, "delete_ratio", "breaking",
                                f"{path}: +{added_lines} -{deleted_lines}", f"delete_ratio:{path}")
            if alert:
                alerts.append(alert)
    calls = leg_terminal_calls(project, task, leg, since=leg.get("last_rule_terminal_at"))
    failure_counts = dict(leg.get("command_failures") or {})
    repeat_fail = int(alert_cfg.get("repeat_fail", 3) or 3)
    for call in calls:
        command = call.get("command", "")
        if call.get("exit_code") not in (0, None):
            failure_counts[command] = int(failure_counts.get(command, 0)) + 1
            if failure_counts[command] >= repeat_fail:
                alert = _rule_alert(leg, "repeat_command_failure", "stall",
                                    f"{command} 连续失败 {failure_counts[command]} 次", f"repeat_command_failure:{command}")
                if alert:
                    alerts.append(alert)
        else:
            failure_counts[command] = 0
    last_file_change = changed_at if entries else float(leg.get("last_file_change_at") or leg.get("started_at") or now())
    stall_minutes = float(alert_cfg.get("stall_minutes", 20) or 20)
    stall_key = f"no_file_change:{last_file_change:.6f}"
    if leg.get("status") == "running" and now() - last_file_change >= stall_minutes * 60:
        alert = _rule_alert(leg, "no_file_change", "stall",
                            f"连续 {int((now() - last_file_change) / 60)} 分钟没有文件变化", stall_key)
        if alert:
            alerts.append(alert)
    log_mtime = os.path.getmtime(project.p("log.md")) if os.path.exists(project.p("log.md")) else leg.get("started_at", now())
    if entries and now() - log_mtime >= 20 * 60:
        log_key = f"log_stale:{last_file_change:.6f}"
        alert = _rule_alert(leg, "log_stale", "record", "有代码改动但 log.md 超过 20 分钟没有新增", log_key)
        if alert:
            alerts.append(alert)
    if entries:
        major_files, major_added, major_deleted = shortstat(store, leg.get("start_ref"), probe_commit) \
            if leg.get("start_ref") else (0, 0, 0)
        thresholds = cfg.get("change_size") or {}
        major_files_threshold = int(thresholds.get("major_files", 4) or 4)
        major_lines_threshold = int(thresholds.get("major_lines", 200) or 200)
        if ((major_files >= major_files_threshold or major_added + major_deleted >= major_lines_threshold)
                and not _new_report_file(project, leg.get("started_at", now()))):
            alert = _rule_alert(leg, "major_without_report", "record",
                                f"本轮改动 {major_files} 个文件、+{major_added} -{major_deleted}，没有新汇报",
                                f"major_without_report:{leg.get('started_at', 0)}")
            if alert:
                alerts.append(alert)
    probe_elapsed = time.monotonic() - probe_started
    _record_rule_state(project, task, leg["n"], {"probe_commit": probe_commit, "probe_ref": probe_ref,
        "last_probe_at": now(), "probe_hashes": updated_hashes, "last_file_change_at": last_file_change,
        "last_rule_terminal_at": max([call["at"] for call in calls] or [leg.get("last_rule_terminal_at") or 0]),
        "command_failures": failure_counts, "alerts": leg.get("alerts", []), "alert_keys": leg.get("alert_keys", []),
        "probe_elapsed_seconds": probe_elapsed,
        "probe_elapsed_total_seconds": float(leg.get("probe_elapsed_total_seconds", 0) or 0) + probe_elapsed,
        "probe_count": int(leg.get("probe_count", 0) or 0) + 1})
    return alerts


EXECUTOR_RESULT_STATUSES = {"done", "decision", "report", "blocked"}


def normalize_executor_result(result):
    """Return a result in the v2 shape while accepting v1 result files.

    The schema now requires ``memory_used``.  Old Codex legs predate that field,
    so reading an old ``last.json`` adds the empty list without changing the
    original file on disk.
    """
    if not isinstance(result, dict):
        return result
    if "memory_used" not in result:
        result = dict(result)
        result["memory_used"] = []
    return result


def validate_executor_result(result, require_memory=True):
    """Small stdlib-only validator shared by Codex and Hermes result parsers."""
    errors = []
    if not isinstance(result, dict):
        return ["输出不是 JSON object"]
    expected = {"status", "summary", "artifact", "change_size", "memory_used"}
    required = expected if require_memory else expected - {"memory_used"}
    missing = sorted(required - set(result))
    extra = sorted(set(result) - expected)
    if missing:
        errors.append("缺少字段：" + ", ".join(missing))
    if extra:
        errors.append("多余字段：" + ", ".join(extra))
    if result.get("status") not in EXECUTOR_RESULT_STATUSES:
        errors.append("status 必须是 done、decision、report 或 blocked")
    for key in ("summary", "artifact"):
        if not isinstance(result.get(key), str):
            errors.append(f"{key} 必须是字符串")
    if result.get("change_size") not in {"none", "minor", "major"}:
        errors.append("change_size 必须是 none、minor 或 major")
    if "memory_used" in result and (not isinstance(result["memory_used"], list)
                                    or any(not isinstance(item, str) for item in result["memory_used"])):
        errors.append("memory_used 必须是字符串数组")
    return errors


def valid_executor_result(result):
    """Whether an output file contains a result that completed the executor call."""
    return not validate_executor_result(normalize_executor_result(result), require_memory=True)


def _capacity_message(value):
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return message
        return " ".join(filter(None, (_capacity_message(value.get("error")),
                                      _capacity_message(value.get("item")))))
    return value if isinstance(value, str) else ""


def _capacity_event(ev):
    """Return text from event types that report a failed Codex call."""
    if not isinstance(ev, dict):
        return ""
    et = ev.get("type")
    if et in ("error", "turn.failed"):
        return _capacity_message(ev.get("error") or ev)
    item = ev.get("item")
    if et == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
        return _capacity_message(item)
    return ""


def is_capacity_failure(events_path, stderr_path, event_offset=0, stderr_offset=0,
                        returncode=0, result=None):
    """Detect a capacity failure using only bytes written by one Codex call.

    A call must have failed (non-zero exit or no valid result file).  The error text is
    deliberately restricted to the newly appended error/turn.failed events and stderr,
    so a capacity message from an earlier retry cannot trigger another retry.
    """
    if returncode == 0 and valid_executor_result(result):
        return False
    events, _ = read_events(events_path, event_offset)
    messages = [_capacity_event(ev) for ev in events]
    stdout = read_bytes_since(events_path, event_offset) or ""
    stderr = read_bytes_since(stderr_path, stderr_offset) or ""
    return any("at capacity" in message.lower() for message in messages + [stdout, stderr])


def capacity_retry_config(cfg):
    retry = ((cfg or {}).get("executor") or {}).get("capacity_retry") or {}
    return {key: retry.get(key, CAPACITY_RETRY_DEFAULTS[key])
            for key in CAPACITY_RETRY_DEFAULTS}


def next_capacity_retry(retry, immediate_used, delayed_used, has_output=False):
    """Backend-independent retry policy: (phase, delay seconds, new counters)."""
    if has_output:
        immediate_used = delayed_used = 0
    if immediate_used < retry["immediate"]:
        return "immediate", 0.0, immediate_used + 1, delayed_used
    if delayed_used < retry["delayed"]:
        return "delayed", float(retry["delayed_minutes"]) * 60, immediate_used, delayed_used + 1
    return None, 0.0, immediate_used, delayed_used


def capacity_wait_seconds(leg, at=None, since=None):
    """Capacity wait time overlapping the current supervision window."""
    if not isinstance(leg, dict):
        return 0.0
    at = now() if at is None else at
    since = leg.get("started_at", 0) if since is None else since
    intervals = leg.get("capacity_wait_intervals", [])
    if intervals:
        total = sum(max(0.0, min(at, interval["ended_at"]) - max(since, interval["started_at"]))
                    for interval in intervals)
    else:
        try:
            total = float(leg.get("capacity_wait_seconds", 0) or 0)
        except (TypeError, ValueError):
            total = 0.0
    wait = leg.get("capacity_wait")
    if isinstance(wait, dict) and wait.get("phase") == "delayed" and wait.get("started_at") is not None:
        total += max(0.0, at - max(since, wait["started_at"]))
    return total


def fmt_usage(u):
    def k(n):
        return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    return f"输入 {k(u['input_tokens'])}（缓存 {k(u['cached_input_tokens'])}），输出 {k(u['output_tokens'])}"


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …"


# ---------------------------------------------------------------- v2 scope / freeze

def _scope_path(value, key):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"范围块 {key} 里的路径必须是非空字符串")
    value = value.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ConfigError(f"范围块 {key} 只能使用项目内相对路径：{value}")
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ConfigError(f"范围块 {key} 不能越出项目目录：{value}")
    return "/".join(parts) or "."


def parse_scope(brief, required=True):
    """Parse the first machine-readable baton-scope block in a task brief.

    Scope is deliberately parsed without Markdown dependencies so the same rules run on
    Python 3.9. Existing v1 tasks may omit the block; new tasks must provide it.
    """
    matches = list(re.finditer(r"^```[ \t]*baton-scope[ \t]*\r?\n(.*?)^```[ \t]*$",
                               brief or "", re.MULTILINE | re.DOTALL))
    if not matches:
        if required:
            raise ConfigError("任务简报缺少 baton-scope 范围块（请在开头加入 JSON 围栏）")
        return None
    raw = matches[0].group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"baton-scope JSON 不合法：第 {e.lineno} 行第 {e.colno} 列：{e.msg}") from e
    if not isinstance(data, dict):
        raise ConfigError("baton-scope 内容必须是 JSON object")
    unknown = sorted(set(data) - SCOPE_KEYS)
    if unknown:
        raise ConfigError("baton-scope 有未知键：" + ", ".join(unknown))
    missing = sorted(SCOPE_LIST_KEYS - set(data))
    if missing:
        raise ConfigError("baton-scope 缺少键：" + ", ".join(missing))
    out = {}
    for key in SCOPE_LIST_KEYS:
        value = data.get(key)
        if not isinstance(value, list):
            raise ConfigError(f"范围块 {key} 必须是 JSON array")
        out[key] = [_scope_path(x, key) for x in value]
    verify = data.get("verify", "auto")
    if verify not in ("auto", "none"):
        raise ConfigError('范围块 verify 只能是 "auto" 或 "none"')
    out["verify"] = verify
    return out


def _safe_rel(project, path):
    path = path.replace("\\", "/")
    full = os.path.realpath(os.path.join(project.root, path))
    root = os.path.realpath(project.root)
    if full != root and not full.startswith(root + os.sep):
        return None
    return os.path.relpath(full, root).replace(os.sep, "/")


def _walk_code_files(project):
    """Yield project-relative files that may be controlled by a freeze rule."""
    root = project.root
    for directory, names, files in os.walk(root):
        rel_dir = os.path.relpath(directory, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_parts = []
        else:
            rel_parts = rel_dir.split("/")
        if any(p == ".git" or p == ".baton" or p == ".council" or p == "node_modules"
               or p.startswith(".venv") or p == "venv" for p in rel_parts):
            names[:] = []
            continue
        names[:] = [n for n in names if n not in (".git", ".baton", ".council", "node_modules", "__pycache__")
                    and not n.startswith(".venv") and n != "venv"
                    and not _shadow_prune_dir(n, rel_dir)]
        for name in files:
            # Bytecode is regenerated by every test run; it is never acceptance material.
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(directory, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            yield (name if rel_dir == "." else rel_dir + "/" + name).replace(os.sep, "/")


def _glob_paths(project, patterns, all_files=None):
    out = set()
    if all_files is None:
        all_files = list(_walk_code_files(project))
    for pattern in patterns or []:
        pattern = pattern.replace("\\", "/")
        full = os.path.join(project.root, pattern)
        if os.path.isdir(full) and not os.path.islink(full):
            for rel in all_files:
                if rel == pattern or rel.startswith(pattern.rstrip("/") + "/"):
                    out.add(rel)
            continue
        matched = []
        if any(c in pattern for c in "*?["):
            import fnmatch
            matched = [p for p in all_files if fnmatch.fnmatch(p, pattern)]
        elif os.path.isfile(full):
            rel = _safe_rel(project, pattern)
            if rel:
                matched = [rel]
        out.update(matched)
    return out


def _section_lines(text, header):
    """Return the exact TOML/INI section text, including its section header."""
    lines = text.splitlines(True)
    begin = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            begin = i
            break
    if begin is None:
        return ""
    end = len(lines)
    for i in range(begin + 1, len(lines)):
        if re.match(r"^\s*\[[^]]+\]\s*$", lines[i]):
            end = i
            break
    return "".join(lines[begin:end])


def _json_selected(path, keys):
    try:
        data = json.loads(read_text(path))
    except (ValueError, OSError):
        return {"__invalid__": read_text(path)}
    if not isinstance(data, dict):
        return {"__invalid__": data}
    out = {}
    for key in keys:
        if key == "scripts":
            scripts = data.get("scripts")
            out[key] = {"test": scripts.get("test")} if isinstance(scripts, dict) and "test" in scripts else None
        else:
            out[key] = data.get(key)
    return out


def _freeze_rule_for(path, root):
    name = os.path.basename(path)
    if name == "pyproject.toml":
        return "pytest-section", _section_lines(read_text(os.path.join(root, path)), "[tool.pytest.ini_options]")
    if name == "setup.cfg":
        return "pytest-section", _section_lines(read_text(os.path.join(root, path)), "[tool:pytest]")
    if name == "package.json":
        return "test-keys", _json_selected(os.path.join(root, path), ("scripts", "jest", "vitest"))
    return "whole", _whole_value(os.path.join(root, path))


def _whole_value(full):
    """Text of a frozen file; a non-UTF-8 file is compared by its byte hash instead of crashing."""
    try:
        return read_text(full, None)
    except UnicodeDecodeError:
        with open(full, "rb") as stream:
            return "sha256:" + hashlib.sha256(stream.read()).hexdigest()


def _hash_value(value):
    if isinstance(value, str):
        raw = value.encode("utf-8", "surrogatepass")
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_config_candidate(path):
    name = os.path.basename(path)
    lower = name.lower()
    if name in FROZEN_WHOLE_NAMES or name in HERMES_NAMES or name == "setup.cfg":
        return True
    if name in ("pyproject.toml", "package.json"):
        return True
    if name.endswith(".pth") or name == "sitecustomize.py":
        return True
    if (lower.startswith(("jest", "vitest", "mocha")) and "config" in lower):
        return True
    if path.startswith(".cursor/rules/") and path.endswith(".mdc"):
        return True
    return False


def _acceptance_new_allowed(path, acceptance):
    import fnmatch
    for pattern in acceptance or []:
        prefix = pattern.rstrip("/")
        if path == prefix or path.startswith(prefix + "/"):
            name = os.path.basename(path)
            if (fnmatch.fnmatch(name, "test_*.py") or fnmatch.fnmatch(name, "*_test.py")
                    or fnmatch.fnmatch(name, "*.spec.*") or fnmatch.fnmatch(name, "*.test.*")):
                return True
    return False


def _test_command_paths(project, command, all_files=None):
    if not command:
        return set()
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    paths = []
    for token in tokens:
        token = token.strip("'\"")
        if not token or token.startswith("-") or token.startswith("/"):
            continue
        if token in ("python", "python3", "pytest", "py.test", "unittest", "discover", "-m"):
            continue
        rel = token.replace("\\", "/")
        full = os.path.join(project.root, rel)
        if os.path.exists(full) or any(c in rel for c in "*?["):
            paths.append(rel)
    return _glob_paths(project, paths, all_files)


def _monitor_parts(path, root):
    """Hashable named parts for monitor-level files."""
    name = os.path.basename(path)
    full = os.path.join(root, path)
    if name in ("pyproject.toml", "setup.cfg"):
        text = read_text(full)
        lines = text.splitlines(True)
        parts = {}
        for i, line in enumerate(lines):
            match = re.match(r"^\s*(\[[^]]+\])\s*$", line)
            if not match:
                continue
            header = match.group(1)
            end = len(lines)
            for j in range(i + 1, len(lines)):
                if re.match(r"^\s*\[[^]]+\]\s*$", lines[j]):
                    end = j
                    break
            parts[header] = _hash_value("".join(lines[i:end]))
        return parts
    if name == "package.json":
        try:
            data = json.loads(read_text(full))
        except (ValueError, OSError):
            return {"<invalid>": _hash_value(read_text(full))}
        return {str(key): _hash_value(value) for key, value in data.items()} if isinstance(data, dict) else {
            "<invalid>": _hash_value(data)}
    return {"<file>": _hash_value(read_text(full))}


def _freeze_manifest(project, scope, cfg):
    all_files = list(_walk_code_files(project))
    frozen = set(_glob_paths(project, scope.get("acceptance", []), all_files))
    frozen.update(_glob_paths(project, scope.get("frozen", []), all_files))
    configured = ((cfg.get("freeze") or {}).get("paths") or [])
    if not isinstance(configured, list) or any(not isinstance(x, str) for x in configured):
        raise ConfigError("freeze.paths 必须是路径字符串数组")
    frozen.update(_glob_paths(project, configured, all_files))
    frozen.update(_test_command_paths(project, (cfg.get("supervision") or {}).get("test_command", ""), all_files))
    monitor = set()
    for path in all_files:
        name = os.path.basename(path)
        if _is_config_candidate(path):
            frozen.add(path)
        if (name.startswith("requirements") and name.endswith(".txt")) or name in DEPENDENCY_NAMES:
            monitor.add(path)
        if name in ("pyproject.toml", "package.json", "setup.cfg"):
            monitor.add(path)
    # Hermes files in the project are lock-level; path-chain files outside the project are WARN-only.
    external = {}
    git_top_dir = git_repo_top(project.root) or project.root
    cur = os.path.dirname(project.root)
    chain = []
    while True:
        chain.append(cur)
        if cur == git_top_dir or cur == os.path.dirname(cur):
            break
        cur = os.path.dirname(cur)
    for directory in reversed(chain):
        for name in HERMES_NAMES:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                rel = os.path.realpath(path)
                if directory == project.root or rel.startswith(os.path.realpath(project.root) + os.sep):
                    continue
                with open(path, "rb") as stream:
                    external[rel] = hashlib.sha256(stream.read()).hexdigest()
    entries = []
    for path in sorted(frozen):
        full = os.path.join(project.root, path)
        if not os.path.isfile(full):
            continue
        rule, value = _freeze_rule_for(path, project.root)
        entries.append({"path": path, "rule": rule, "sha256": _hash_value(value), "value": value})
    monitor_entries = []
    for path in sorted(monitor):
        full = os.path.join(project.root, path)
        try:
            with open(full, "rb") as stream:
                full_hash = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            continue
        monitor_entries.append({"path": path, "sha256": full_hash,
                                "parts": _monitor_parts(path, project.root)})
    return {"entries": entries, "monitor": sorted(monitor), "monitor_entries": monitor_entries,
            "external": external,
            "acceptance": list(scope.get("acceptance", [])), "frozen": list(scope.get("frozen", [])),
            "configured": configured}


def freeze_dir(project, task):
    return os.path.join(project.state_dir, "frozen", task)


def write_freeze_baseline(project, task, scope, cfg):
    manifest = _freeze_manifest(project, scope, cfg)
    directory = freeze_dir(project, task)
    files_dir = os.path.join(directory, "files")
    os.makedirs(files_dir, exist_ok=True)
    for entry in manifest["entries"]:
        source = os.path.join(project.root, entry["path"])
        dest = os.path.join(files_dir, entry["path"])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(source, dest)
    write_json(os.path.join(directory, "manifest.json"), manifest)
    return manifest


def _baseline_value(project, entry):
    if entry.get("rule") == "whole":
        path = os.path.join(project.root, entry["path"])
        return _whole_value(path) if os.path.isfile(path) else None
    path = os.path.join(project.root, entry["path"])
    if entry.get("rule") == "pytest-section":
        return _section_lines(read_text(path), "[tool.pytest.ini_options]" if path.endswith("pyproject.toml") else "[tool:pytest]")
    if entry.get("rule") == "test-keys":
        return _json_selected(path, ("scripts", "jest", "vitest"))
    return read_text(path, None)


def check_freeze(project, task, st=None):
    """Compare the worktree with the project-external freeze baseline."""
    st = st or project.read_state(task, {}) or {}
    manifest = st.get("freeze_manifest")
    if not isinstance(manifest, dict):
        manifest = read_json(os.path.join(freeze_dir(project, task), "manifest.json"), {}) or {}
    if not manifest or not isinstance(manifest, dict) or "entries" not in manifest:
        return {"violations": [], "monitor": [], "external_warn": []}
    entries = {e.get("path"): e for e in manifest.get("entries", []) if isinstance(e, dict) and e.get("path")}
    violations, monitored = [], []
    current = set(_walk_code_files(project))
    acceptance = manifest.get("acceptance") or []
    for path, entry in entries.items():
        full = os.path.join(project.root, path)
        if not os.path.isfile(full):
            violations.append({"path": path, "type": "删除", "rule": entry.get("rule", "whole")})
            continue
        value = _baseline_value(project, entry)
        if _hash_value(value) != entry.get("sha256"):
            violations.append({"path": path, "type": "改动", "rule": entry.get("rule", "whole")})
    for path in sorted(current - set(entries)):
        if _is_config_candidate(path) and not _acceptance_new_allowed(path, acceptance):
            violations.append({"path": path, "type": "新建", "rule": "测试/上下文配置"})
    # An explicitly frozen path that was absent at start is still protected against creation.
    patterns = list(st.get("scope", {}).get("frozen", []) or []) + list(manifest.get("configured", []) or [])
    import fnmatch
    for path in sorted(current):
        if path in entries:
            continue
        if any(fnmatch.fnmatch(path, p) or path == p or path.startswith(p.rstrip("/") + "/")
               for p in patterns):
            violations.append({"path": path, "type": "新建", "rule": "范围块 frozen/freeze.paths"})
    monitor_entries = manifest.get("monitor_entries") or [
        {"path": path, "sha256": None, "parts": {}} for path in manifest.get("monitor", [])
    ]
    for monitor_entry in monitor_entries:
        path = monitor_entry.get("path")
        if not path:
            continue
        full = os.path.join(project.root, path)
        if not os.path.isfile(full):
            monitored.append({"path": path, "type": "删除", "rule": "监看级", "parts": ["整文件"]})
            continue
        with open(full, "rb") as stream:
            current_hash = hashlib.sha256(stream.read()).hexdigest()
        if monitor_entry.get("sha256") == current_hash:
            continue
        old_parts = monitor_entry.get("parts") or {}
        new_parts = _monitor_parts(path, project.root)
        changed = sorted(set(old_parts) | set(new_parts))
        changed = [part for part in changed if old_parts.get(part) != new_parts.get(part)]
        monitored.append({"path": path, "type": "改动", "rule": "监看级",
                          "parts": changed or ["整文件"]})
    external_warn = []
    for path, old_hash in (manifest.get("external") or {}).items():
        try:
            with open(path, "rb") as stream:
                current_hash = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            current_hash = None
        if current_hash != old_hash:
            external_warn.append(path)
    return {"violations": violations, "monitor": monitored, "external_warn": external_warn}


def freeze_summary(check):
    return "; ".join(f"{x['path']}（{x['type']}，{x['rule']}）" for x in check.get("violations", []))


def config_input_hash(project):
    parts = []
    for path, exists in project.config_paths():
        if exists:
            try:
                with open(path, "rb") as stream:
                    parts.append((path, hashlib.sha256(stream.read()).hexdigest()))
            except OSError:
                parts.append((path, None))
    return _hash_value(parts)


def config_for_state(project, st):
    snapshot = st.get("config_snapshot") if isinstance(st, dict) else None
    return snapshot if isinstance(snapshot, dict) else project.config


def config_different_keys(current, snapshot, prefix=""):
    """List changed effective config keys without printing their values."""
    if isinstance(current, dict) and isinstance(snapshot, dict):
        changed = []
        for key in sorted(set(current) | set(snapshot)):
            if not prefix and key == "project":
                continue
            dotted = f"{prefix}.{key}" if prefix else key
            if key not in current or key not in snapshot:
                changed.append(dotted)
            else:
                changed.extend(config_different_keys(current[key], snapshot[key], dotted))
        return changed
    return [prefix] if current != snapshot else []


VERIFY_COUNT_KEYS = ("passed", "failed", "skipped", "xfailed", "xpassed", "error", "deselected")


def _verification_enabled(st, cfg):
    scope = st.get("scope") if isinstance(st, dict) else None
    if not scope or scope.get("verify", "auto") == "none":
        return False
    return bool((cfg.get("supervision") or {}).get("test_command"))


def _shell_tokens(command):
    try:
        lexer = shlex.shlex(command or "", posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return (command or "").split()


def _is_pytest_command(command):
    tokens = _shell_tokens(command)
    if any(token in {"&&", "||", ";", "|", "&"} for token in tokens):
        return False
    return "pytest" in tokens or any(token.endswith("/pytest") for token in tokens) \
        or bool(re.search(r"(?:^|\s)pytest(?:\s|$)", command or ""))


def _junit_nodeid(case, project_root=None):
    """Rebuild pytest node ids from xunit1 or xunit2 testcase attributes."""
    file_path = case.attrib.get("file") or case.attrib.get("path")
    classname = case.attrib.get("classname", "")
    name = case.attrib.get("name", "")
    class_parts = []
    if file_path:
        file_path = file_path.replace("\\", "/")
        if project_root and os.path.isabs(file_path):
            try:
                file_path = os.path.relpath(file_path, project_root).replace(os.sep, "/")
            except ValueError:
                pass
        module = os.path.splitext(file_path)[0].replace("/", ".")
        if classname == module:
            class_parts = []
        elif module and classname.startswith(module + "."):
            class_parts = [part for part in classname[len(module) + 1:].split(".") if part]
        else:
            # Some pytest plugins emit a short classname; retain only the class suffix
            # after the module portion when the module is not repeated.
            class_parts = [part for part in classname.split(".") if part and part != module]
    elif classname:
        parts = [part for part in classname.split(".") if part]
        if project_root:
            for cut in range(len(parts), 0, -1):
                candidate = os.path.join(project_root, *parts[:cut]) + ".py"
                if os.path.isfile(candidate):
                    file_path = "/".join(parts[:cut]) + ".py"
                    class_parts = parts[cut:]
                    break
        if not file_path:
            # Keep a deterministic fallback for plugins that omit file and for
            # callers that do not provide a project root.
            file_path = "/".join(parts)
    if not file_path:
        return name or "<unknown>"
    bits = [file_path]
    bits.extend(class_parts)
    if name:
        bits.append(name)
    return "::".join(bits)


def _parse_junit(path, acceptance=None, project_root=None):
    counts = {key: 0 for key in VERIFY_COUNT_KEYS}
    tests = []
    if not os.path.isfile(path):
        return counts, tests
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return counts, tests
    for case in root.iter():
        if case.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        nodeid = _junit_nodeid(case, project_root)
        status = case.attrib.get("status", "")
        children = {child.tag.rsplit("}", 1)[-1]: child for child in case}
        skipped = children.get("skipped")
        failure = children.get("failure")
        if skipped is not None and skipped.attrib.get("type") == "pytest.xfail":
            status = "xfailed"
        elif failure is not None and (failure.attrib.get("message") or "").startswith("[XPASS(strict)]"):
            status = "xpassed"
        elif any(child.tag.rsplit("}", 1)[-1] == "error" for child in case):
            status = "error"
        elif failure is not None:
            status = "failed"
        elif skipped is not None:
            status = "skipped"
        elif status not in VERIFY_COUNT_KEYS:
            status = "passed"
        if status not in counts:
            status = "passed"
        counts[status] += 1
        tests.append({"nodeid": nodeid, "status": status})
    return counts, tests


def _acceptance_failures(tests, acceptance):
    failures = []
    for prefix in acceptance or []:
        prefix = prefix.rstrip("/")
        matched = [test for test in tests if test["nodeid"] == prefix
                   or test["nodeid"].startswith(prefix + "/")
                   or test["nodeid"].startswith(prefix + "::")]
        if not matched:
            failures.append({"nodeid": prefix, "reason": "not collected"})
            continue
        for test in matched:
            if test["status"] in ("skipped", "xfailed", "failed", "error"):
                failures.append({"nodeid": test["nodeid"], "reason": test["status"]})
    return failures


def _verification_diff(current, baseline):
    cur = current.get("counts") if isinstance(current, dict) else None
    base = baseline.get("counts") if isinstance(baseline, dict) else None
    if not isinstance(cur, dict) or not isinstance(base, dict):
        return None
    cur_total = sum(int(cur.get(key, 0) or 0) for key in VERIFY_COUNT_KEYS)
    base_total = sum(int(base.get(key, 0) or 0) for key in VERIFY_COUNT_KEYS)
    skipped_up = int(cur.get("skipped", 0) or 0) > int(base.get("skipped", 0) or 0)
    total_down = cur_total < base_total
    warnings = []
    if total_down:
        warnings.append("测试总数下降")
    if skipped_up:
        warnings.append("跳过数上升")
    return {"baseline_total": base_total, "current_total": cur_total,
            "total_delta": cur_total - base_total,
            "skipped_delta": int(cur.get("skipped", 0) or 0) - int(base.get("skipped", 0) or 0),
            "warnings": warnings}


def _verification_summary(verify):
    if not verify:
        return "无验证记录"
    if verify.get("skipped"):
        return verify.get("skip_reason") or "本任务没有 baton 亲跑的验证"
    counts = verify.get("counts")
    count_text = "退出码=" + str(verify.get("exit_code"))
    if isinstance(counts, dict):
        count_text += "，" + ", ".join(f"{key}={counts.get(key, 0)}" for key in VERIFY_COUNT_KEYS)
    count_text += f"，验收失败={len(verify.get('acceptance_failed') or [])}"
    if verify.get("counts_incomplete"):
        count_text += "，计数不完整"
    diff = verify.get("baseline_diff")
    if diff and diff.get("warnings"):
        count_text += "，警告=" + "/".join(diff["warnings"])
    return count_text


def run_verification(project, task, st, cfg, leg_n=None, baseline=False, skip_reason=None):
    """Run the baton-owned verifier and persist its JSON record."""
    scope = st.get("scope") or {}
    command = (cfg.get("supervision") or {}).get("test_command") or ""
    if baseline:
        path = os.path.join(freeze_dir(project, task), "baseline.verify.json")
    else:
        path = project.leg_path(task, leg_n, "verify.json")
    result = {"task": task, "leg": leg_n, "baseline": baseline, "command": command,
              "counts": None, "tests": [], "acceptance_failed": [], "baseline_diff": None,
              "counts_incomplete": False, "skipped": False, "skip_reason": None,
              "exit_code": None, "timed_out": False, "output_tail": ""}
    if skip_reason or not _verification_enabled(st, cfg):
        result["skipped"] = True
        result["skip_reason"] = skip_reason or "本任务没有 baton 亲跑的验证"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json(path, result)
        if not baseline:
            external = os.path.join(project.state_dir, "runs", task, os.path.basename(path))
            os.makedirs(os.path.dirname(external), exist_ok=True)
            shutil.copy2(path, external)
        return result
    is_pytest = _is_pytest_command(command)
    junit = None
    run_command = command
    if is_pytest:
        junit = project.leg_path(task, leg_n, "junit.xml") if leg_n else os.path.join(freeze_dir(project, task), "baseline.junit.xml")
        run_command += " --junitxml=" + (subprocess.list2cmdline([junit]) if os.name == "nt" else shlex.quote(junit)) + " -rA -o junit_family=xunit1"
        result["junitxml"] = junit
        os.makedirs(os.path.dirname(junit), exist_ok=True)
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        result["counts_incomplete"] = "-x" in tokens or "--exitfirst" in tokens
    elif any(token in {"&&", "||", ";", "|", "&"} for token in _shell_tokens(command)):
        result["verification_note"] = "复合命令，未追加 junit"
    started = now()
    try:
        timeout = float((cfg.get("supervision") or {}).get("verify_timeout_minutes", 30)) * 60
        proc = platform_support.detached_popen(run_command, cwd=project.root, shell=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8")
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as e:
            result["timed_out"] = True
            result["exit_code"] = None
            output = ((e.stdout or "") if isinstance(e.stdout, str) else "") \
                + ((e.stderr or "") if isinstance(e.stderr, str) else "")
            try:
                platform_support.terminate_group(proc)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                more_stdout, more_stderr = proc.communicate(timeout=5)
                output += (more_stdout or "") + (more_stderr or "")
            except subprocess.TimeoutExpired as e2:
                output += ((e2.stdout or "") if isinstance(e2.stdout, str) else "") \
                    + ((e2.stderr or "") if isinstance(e2.stderr, str) else "")
                try:
                    platform_support.terminate_group(proc, force=True)
                except (ProcessLookupError, PermissionError):
                    pass
                more_stdout, more_stderr = proc.communicate()
                output += (more_stdout or "") + (more_stderr or "")
            result["output_tail"] = output
        else:
            result["exit_code"] = proc.returncode
            result["output_tail"] = (stdout or "") + (stderr or "")
    except subprocess.TimeoutExpired as e:
        result["timed_out"] = True
        result["exit_code"] = None
        result["output_tail"] = ((e.stdout or "") if isinstance(e.stdout, str) else "") \
            + ((e.stderr or "") if isinstance(e.stderr, str) else "")
    result["output_tail"] = result["output_tail"][-4000:]
    if is_pytest:
        counts, tests = _parse_junit(junit, scope.get("acceptance", []), project.root)
        result["counts"], result["tests"] = counts, tests
        deselected = re.search(r"(\d+)\s+deselected", result["output_tail"], re.I)
        if deselected:
            count = int(deselected.group(1))
            counts["deselected"] += count
        result["acceptance_failed"] = _acceptance_failures(tests, scope.get("acceptance", []))
        if tests and scope.get("acceptance") and not any(
                test["nodeid"].startswith(str(prefix).rstrip("/") + "/")
                or test["nodeid"].startswith(str(prefix).rstrip("/") + "::")
                or test["nodeid"] == str(prefix).rstrip("/")
                for test in tests for prefix in scope.get("acceptance", [])):
            result["acceptance_hint"] = "已收集到测试，但没有匹配验收前缀；可能是 pytest rootdir 不在项目根。"
    if not baseline:
        baseline_result = read_json(os.path.join(freeze_dir(project, task), "baseline.verify.json"), {}) or {}
        result["baseline_diff"] = _verification_diff(result, baseline_result)
    result["duration_seconds"] = round(now() - started, 3)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, result)
    if not baseline:
        external = os.path.join(project.state_dir, "runs", task, os.path.basename(path))
        os.makedirs(os.path.dirname(external), exist_ok=True)
        shutil.copy2(path, external)
    return result


def cmd_restore_frozen(project, args):
    project.require_init()
    st = project.load_state(args.task)
    check = check_freeze(project, args.task, st)
    selected = set(args.paths or [])
    violations = check["violations"] if not selected else [x for x in check["violations"] if x["path"] in selected]
    if selected and len(violations) != len(selected):
        missing = sorted(selected - {x["path"] for x in violations})
        die("指定路径当前没有冻结违规：" + ", ".join(missing))
    if not violations:
        print("没有需要恢复的冻结违规")
        return
    baseline = {e["path"]: e for e in (st.get("freeze_manifest") or {}).get("entries", [])}
    root = freeze_dir(project, args.task)
    files_dir = os.path.join(root, "files")
    backup_dir = os.path.join(root, "restore-backup", stamp(fmt="%Y%m%d-%H%M%S"))
    restored, backed = [], []
    for item in violations:
        path = item["path"]
        full = os.path.join(project.root, path)
        if os.path.lexists(full) and item["type"] == "新建":
            dest = os.path.join(backup_dir, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(full, dest)
            backed.append(path)
            continue
        entry = baseline.get(path)
        source = os.path.join(files_dir, path)
        if entry and os.path.isfile(source):
            os.makedirs(os.path.dirname(full), exist_ok=True)
            if entry.get("rule") == "whole":
                shutil.copy2(source, full)
            elif entry.get("rule") == "pytest-section":
                header = "[tool.pytest.ini_options]" if os.path.basename(path) == "pyproject.toml" else "[tool:pytest]"
                current = read_text(full)
                baseline_text = entry.get("value") or ""
                lines = current.splitlines(True)
                start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
                end = len(lines)
                if start is not None:
                    for i in range(start + 1, len(lines)):
                        if re.match(r"^\s*\[[^]]+\]\s*$", lines[i]):
                            end = i
                            break
                    lines[start:end] = [baseline_text] if baseline_text else []
                elif baseline_text:
                    if lines and not lines[-1].endswith("\n"):
                        lines.append("\n")
                    lines.append(baseline_text)
                with open(full, "w", encoding="utf-8") as stream:
                    stream.write("".join(lines))
            elif entry.get("rule") == "test-keys":
                current = read_json(full, {})
                base_value = entry.get("value") or {}
                if isinstance(current, dict) and isinstance(base_value, dict):
                    if "scripts" in base_value:
                        base_scripts = base_value.get("scripts")
                        scripts = current.setdefault("scripts", {})
                        if isinstance(scripts, dict) and isinstance(base_scripts, dict) and "test" in base_scripts:
                            scripts["test"] = base_scripts["test"]
                        elif isinstance(scripts, dict):
                            scripts.pop("test", None)
                    for key in ("jest", "vitest"):
                        if key in base_value and base_value[key] is not None:
                            current[key] = base_value[key]
                        elif key not in base_value or base_value[key] is None:
                            current.pop(key, None)
                    write_json(full, current)
                else:
                    shutil.copy2(source, full)
            restored.append(path)
    print(f"已恢复冻结文件 {len(restored)} 个；违规新建文件移到项目外备份 {len(backed)} 个。")
    if backed:
        print(f"备份目录：{backup_dir}")


def _set_dotted(obj, key, value):
    parts = key.split(".")
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        raise ConfigError(f"配置键无效：{key}")
    cur = obj
    for part in parts[:-1]:
        if not isinstance(cur, dict):
            raise ConfigError(f"配置路径不是 object：{key}")
        cur = cur.setdefault(part, {})
    if not isinstance(cur, dict):
        raise ConfigError(f"配置路径不是 object：{key}")
    cur[parts[-1]] = value


def _get_dotted(obj, key, default=None):
    cur = obj
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _normalize_amendments(items):
    """Read both v2 object amendments and the early string-list representation."""
    out = []
    for item in items or []:
        if isinstance(item, str):
            out.append({"id": os.path.basename(item).split(".", 1)[0], "path": item,
                        "after_leg": 0, "relax": False})
        elif isinstance(item, dict) and item.get("path"):
            out.append({"id": item.get("id") or os.path.basename(item["path"]).split(".", 1)[0],
                        "path": item["path"], "after_leg": int(item.get("after_leg") or 0),
                        "relax": bool(item.get("relax"))})
    return out


def amendments_text(st):
    lines = []
    for amendment in _normalize_amendments(st.get("amendments")):
        after_leg = amendment["after_leg"]
        lines.append(f"标准在第 {after_leg} 轮后改过：{amendment['id']}（第 {after_leg} 轮后生效）")
        text = read_text(amendment["path"])
        lines.append(text[:4000])
        if len(text) > 4000:
            lines.append("（正文超过 4000 字，已截断）")
        lines.append(f"完整路径：{amendment['path']}")
    return "\n".join(lines) + ("\n" if lines else "")


def _config_assignment(value):
    if "=" not in value:
        raise ConfigError(f"--config 需要 键=值：{value}")
    key, raw = value.split("=", 1)
    if not key:
        raise ConfigError(f"--config 键不能为空：{value}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return key, parsed


def _open_tty():
    return platform_support.open_tty()


def _require_task_confirmation(task):
    try:
        tty = _open_tty()
    except OSError as e:
        raise ConfigError(f"需要在真实终端逐字确认任务名；无法打开 {platform_support.tty_name()}：{e}") from e
    try:
        tty.write(f"确认放宽任务 {task}？请逐字输入任务名：")
        tty.flush()
        answer = tty.readline().strip()
    finally:
        tty.close()
    if answer != task:
        raise ConfigError("任务名确认不匹配，未执行放宽操作")


def _adopt_freeze_entries(project, task, manifest, paths):
    entries = {e.get("path"): e for e in manifest.get("entries", []) if isinstance(e, dict)}
    files_dir = os.path.join(freeze_dir(project, task), "files")
    for path in paths:
        rel = _safe_rel(project, path)
        if not rel:
            raise ConfigError(f"--adopt 路径必须在项目内：{path}")
        full = os.path.join(project.root, rel)
        if not os.path.isfile(full):
            raise ConfigError(f"--adopt 路径不存在或不是文件：{rel}")
        rule, value = _freeze_rule_for(rel, project.root)
        entry = {"path": rel, "rule": rule, "sha256": _hash_value(value), "value": value}
        entries[rel] = entry
        dest = os.path.join(files_dir, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(full, dest)
    manifest["entries"] = [entries[path] for path in sorted(entries)]
    return manifest


def cmd_amend(project, args):
    project.require_init()
    check_task(args.task)
    st = project.load_state(args.task)
    leg = current_leg(st)
    if leg and (leg.get("status") == "running" or codex_alive(leg)):
        die("当前轮仍在运行，先 baton stop；amend 只能在没有运行中的轮次时执行")
    assignments = [_config_assignment(item) for item in (args.config or [])]
    amendment_text = read_text(args.amendment_file) if args.amendment_file else ""
    if args.amendment_file and not os.path.isfile(args.amendment_file):
        die(f"找不到修正案文件 {args.amendment_file}")
    old_snapshot = config_for_state(project, st)
    snapshot = json.loads(json.dumps(old_snapshot))
    for key, value in assignments:
        if key == "mode" or key.startswith("mode."):
            raise ConfigError("不能用 amend --config 修改 mode；lean/standard 要开新任务")
        if key == "project" or key.startswith("project."):
            raise ConfigError("不能用 amend --config 修改 project；项目身份由 Baton 管理")
        _set_dotted(snapshot, key, value)
    validate_config(snapshot)
    manifest = st.get("freeze_manifest") or read_json(os.path.join(freeze_dir(project, args.task), "manifest.json"), {}) or {}
    checked = check_freeze(project, args.task, st)
    violation_paths = {item["path"]: item for item in checked.get("violations", [])}
    adopt = [_safe_rel(project, path) for path in (args.adopt or [])]
    if any(path is None for path in adopt):
        die("--adopt 路径必须在项目内")
    adopt = [path for path in adopt if path]
    unknown_adopt = sorted(set(adopt) - set(violation_paths))
    if unknown_adopt:
        die("--adopt 路径当前没有冻结违规：" + ", ".join(unknown_adopt))
    for path in adopt:
        if not os.path.isfile(os.path.join(project.root, path)):
            die(f"--adopt 路径不存在或不是文件：{path}")

    def weakens(key, value):
        if key in ("supervision.test_command", "supervision.verify_timeout_minutes", "supervision", "freeze") or key.startswith("freeze."):
            if key == "freeze.check_seconds":
                old = _get_dotted(old_snapshot, key, 60)
                return value >= old
            return True
        return False
    relax = bool(adopt or any(weakens(key, value) for key, value in assignments)
                 or re.search(r"(?:^|\n)\s*relax\s*:\s*true\b", amendment_text, re.I))
    non_adopt_violations = [path for path in violation_paths if path not in adopt]
    if non_adopt_violations:
        die("执行 amend 前必须先处理其他冻结违规：" + ", ".join(sorted(non_adopt_violations)))
    if not (assignments or amendment_text or adopt):
        die("amend 至少需要修正案文件、--config 或 --adopt")
    if relax and not args.user_approved:
        die("这是放宽冻结或验证标准的操作，请加 --user-approved 并在真实终端逐字确认任务名")
    if args.user_approved:
        _require_task_confirmation(args.task)
    if adopt:
        manifest = _adopt_freeze_entries(project, args.task, manifest, adopt)
    amendment_path = None
    if amendment_text:
        amendments_dir = os.path.join(freeze_dir(project, args.task), "amendments")
        os.makedirs(amendments_dir, exist_ok=True)
        existing = [name for name in os.listdir(amendments_dir) if re.fullmatch(r"A\d{3}\.md", name)]
        n = max([int(name[1:4]) for name in existing] or [0]) + 1
        amendment_path = os.path.join(amendments_dir, f"A{n:03d}.md")
        with open(amendment_path, "w", encoding="utf-8") as stream:
            stream.write(amendment_text)

    state_update = {"config_snapshot": snapshot, "freeze_manifest": manifest,
                    "frozen_dir": freeze_dir(project, args.task)}
    if assignments:
        state_update["config_input_hash"] = config_input_hash(project)
    if amendment_path:
        state_update["amendments"] = _normalize_amendments(st.get("amendments")) + [{
            "id": os.path.splitext(os.path.basename(amendment_path))[0],
            "path": amendment_path, "after_leg": len(st.get("legs", [])), "relax": relax,
        }]
    project.update_state(args.task, lambda state: state.update(state_update))
    write_json(os.path.join(freeze_dir(project, args.task), "manifest.json"), manifest)
    print(f"已登记修正案/配置修正；标准在第 {len(st.get('legs', []))} 轮后改过。")
    if amendment_path:
        print(f"修正案：{amendment_path}")
    if adopt:
        print("已重新冻结：" + ", ".join(adopt))


# ---------------------------------------------------------------- prompts

def fill(template, mapping):
    for k, v in mapping.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def protocol_text(project, task):
    cfg = config_for_state(project, project.read_state(task, {}) or {})
    protocol_name = "executor-protocol-lean.md" if cfg.get("mode") == "lean" else "executor-protocol.md"
    return fill(read_text(os.path.join(REFERENCES, protocol_name)), {
        "TASK": task,
        "BATON_DIR": project.dir,
        "LOG": project.p("log.md"),
        "REPORTS": project.p("reports"),
        "DECISIONS": project.p("decisions"),
        "REPORT_TEMPLATE": os.path.join(TEMPLATES, "report.html"),
        "DECISION_TEMPLATE": os.path.join(TEMPLATES, "decision.md"),
        "MAJOR_FILES": cfg["change_size"]["major_files"],
        "MAJOR_LINES": cfg["change_size"]["major_lines"],
        "LANGUAGE": cfg.get("language", "中文"),
        "TEST_COMMAND": cfg["supervision"].get("test_command") or "（未配置，按简报中的验证方式）",
    })


def codex_command(project, kind, thread_id, last_path, cfg=None):
    ex = (cfg or project.config)["executor"]
    cmd = [platform_support.executable("codex"), "exec"] + (["resume"] if kind == "resume" else [])
    cmd += ["--json", "-m", ex["model"],
            "-c", f'model_reasoning_effort="{ex["reasoning_effort"]}"',
            "-c", f'service_tier="{ex["service_tier"]}"',
            "-c", f'sandbox_mode="{ex["sandbox"]}"',
            "-c", 'approval_policy="never"',
            "--output-schema", os.path.join(TEMPLATES, "leg-result.schema.json"),
            "-o", last_path]
    if ex["sandbox"] == "workspace-write":
        net = "true" if ex.get("network_access") else "false"
        cmd += ["-c", f"sandbox_workspace_write.network_access={net}"]
    if ex.get("disable_fast_mode"):
        cmd += ["--disable", "fast_mode"]
    for kv in ex.get("extra_config") or []:
        cmd += ["-c", kv]
    if not is_git(project.root):
        cmd += ["--skip-git-repo-check"]
    if kind == "start":
        cmd += ["-C", project.root, "-"]
    else:
        cmd += [thread_id, "-"]
    return cmd


def hermes_sandbox_defines(project, cfg):
    """Seatbelt -D values.  Keep every path explicit so the rule is reusable."""
    profile = hermes_profile_dir(cfg)
    home = os.path.expanduser("~")
    baton_home = os.path.dirname(os.path.dirname(project.state_dir))
    values = {
        "PROJECT": project.root,
        "PROFILE_DIR": profile,
        "TMP_DIR": "/private/tmp",
        "VAR_TMP_DIR": "/private/var/tmp",
        "TMP_FOLDERS_DIR": "/private/var/folders",
        "CACHE_HOME": os.path.join(home, "Library", "Caches"),
        "USER_CACHE": os.path.join(home, ".cache"),
        "BATON_TASKS": project.p("tasks"),
        "BATON_REVIEWS": project.p("reviews"),
        "PROFILE_SOUL": os.path.join(profile, "SOUL.md"),
        "PROFILE_CONFIG": os.path.join(profile, "config.yaml"),
        "PROFILE_ENV": os.path.join(profile, ".env"),
        "BATON_HOME": baton_home,
        "CLAUDE_HOME": os.path.join(home, ".claude"),
    }
    return {key: canonical_dir(value) for key, value in values.items()}


def hermes_command(project, kind, thread_id, prompt, cfg=None):
    """Build the exact Hermes invocation used by one executor leg."""
    cfg = cfg or project.config
    ex = cfg["executor"]
    profile = ex.get("hermes_profile", "baton-exec-v2")
    args = []
    if ex.get("sandbox_exec", True):
        args += ["sandbox-exec", "-f", os.path.join(TEMPLATES, "hermes-sandbox.sb")]
        for key, value in hermes_sandbox_defines(project, cfg).items():
            args += ["-D", f"{key}={value}"]
    args += [hermes_executable(cfg), "-p", profile, "chat"]
    if kind == "resume":
        if not thread_id:
            raise ConfigError("Hermes resume 缺少 session_id")
        args += ["-r", str(thread_id)]
    toolsets = list(ex.get("toolsets", ["file", "terminal", "todo"]))
    if memory_installed(cfg) and "baton-memory" not in toolsets:
        toolsets.append("baton-memory")
    if not memory_installed(cfg):
        toolsets = [item for item in toolsets if item != "baton-memory"]
    args += ["-Q", "--query=" + str(prompt), "--provider", ex.get("provider", "openai-codex"),
            "-m", ex["model"], "--reasoning", ex["reasoning_effort"], "--source", "tool",
            "--yolo", "-t", ",".join(toolsets)]
    return args


def hermes_executor_env(project, task, leg_n, started_at, cfg, st=None):
    st = st or {}
    env = _hermes_subprocess_env(cfg)
    env.update({"MEM0_TELEMETRY": "false", "BATON_HOME": memory_baton_home(cfg),
                "BATON_TASK": str(task), "BATON_LEG": str(leg_n),
                "BATON_LEG_STARTED": str(started_at),
                "HERMES_EPHEMERAL_SYSTEM_PROMPT": hermes_ephemeral_system_prompt(project, task, st)})
    return env


def _memory_keyword_fallback(project, task, prompt, st, cfg, reason):
    """System-Python fallback: keyword route only, with no mem0 import."""
    from baton_memory.ledger import MemoryLedger
    from baton_memory.search import render_memory_context, render_pinned_block, keyword_search
    ledger = MemoryLedger(project, baton_home=memory_baton_home(cfg))
    if not os.path.exists(ledger.ledger_path):
        return prompt, {"pushed": [], "core": "", "context": "", "error": reason}
    good, error = ledger.verify(repair=True)
    if not good:
        return prompt, {"pushed": [], "core": "", "error": "记忆台账校验失败：%s" % error}
    brief = read_text(os.path.join(project.task_dir(task), "brief.md"))
    scope = (st or {}).get("scope") or parse_scope(brief, required=False) or {}
    queries = [brief[:1200], str(prompt)[:1200]] + [str(x) for x in scope.get("allow", [])[:8]]
    prior = (st or {}).get("legs") or []
    if prior:
        diff_items = (prior[-1].get("diff") or []) if isinstance(prior[-1], dict) else []
        queries.extend(str(item.get("path")) for item in diff_items if isinstance(item, dict) and item.get("path"))
    previous_ids = [str(item) for leg in (st or {}).get("legs", []) for item in (leg.get("memory_pushed") or [])]
    latest = ledger.latest()
    withdrawals = []
    for ident in dict.fromkeys(previous_ids):
        item = latest.get(ident) or {}
        if item.get("status") == "superseded":
            withdrawals.append("撤回：%s 已由 %s 取代，忽略此前轮次推送的 %s" % (ident, item.get("replaced_by", "未知条目"), ident))
        elif item.get("status") in {"retired", "suspended"}:
            withdrawals.append("撤回：%s 已%s，忽略此前轮次推送的 %s" % (ident, item.get("status"), ident))
    memory_cfg = (cfg or project.config).get("memory", {})
    _, _, core_ids = render_pinned_block(ledger, budget=int(memory_cfg.get("core_max_chars", 2200)))
    keyword = []
    for query in queries:
        keyword.extend(keyword_search(ledger, query, top_k=30))
    from baton_memory.search import fuse_results
    results = fuse_results(ledger, keyword, [], top_k=int(memory_cfg.get("inject_top_k", 10)), core_ids=core_ids)
    context = render_memory_context(ledger, results, withdrawals=withdrawals)
    core, truncated, _ = render_pinned_block(ledger, budget=int(memory_cfg.get("core_max_chars", 2200)))
    return str(prompt).rstrip() + "\n\n" + context, {"pushed": [str(x.get("ledger_id")) for x in results],
        "core": core, "context": context, "withdrawals": withdrawals, "core_truncated": truncated,
        "fallback_reason": reason, "error": ""}


def memory_prompt_for_leg(project, task, prompt, st=None, cfg=None):
    """Compute the complete memory injection in the installed venv subprocess."""
    cfg = cfg or project.config
    python = None if (cfg.get("memory") or {}).get("backend") == "local" else memory_venv_python(cfg)
    if not python:
        return _memory_keyword_fallback(project, task, prompt, st or {}, cfg, "本轮没有记忆工具（venv 不存在）")
    brief = read_text(os.path.join(project.task_dir(task), "brief.md"))
    scope = (st or {}).get("scope") or parse_scope(brief, required=False) or {}
    queries = [brief[:1200], str(prompt)[:1200]] + [str(x) for x in scope.get("allow", [])[:8]]
    prior = (st or {}).get("legs") or []
    if prior:
        diff_items = (prior[-1].get("diff") or []) if isinstance(prior[-1], dict) else []
        queries.extend(str(item.get("path")) for item in diff_items if isinstance(item, dict) and item.get("path"))
    payload = {"project_root": project.root, "baton_home": memory_baton_home(cfg), "queries": queries,
               "previous_ids": [str(item) for leg in (st or {}).get("legs", []) for item in (leg.get("memory_pushed") or [])],
               "memory": cfg.get("memory", {})}
    env = os.environ.copy()
    env.update({"BATON_HOME": memory_baton_home(cfg), "MEM0_TELEMETRY": "false"})
    try:
        result = subprocess.run([python, "-m", "baton_memory.prompt"], cwd=project.root, env=env,
                                input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                text=True, timeout=25, encoding="utf-8")
        value = json.loads(result.stdout.strip().splitlines()[-1]) if result.stdout.strip() else {}
        if not result.returncode or value.get("ok"):
            if value.get("ok"):
                return str(prompt).rstrip() + "\n\n" + value.get("context", ""), value
            reason = value.get("error", "记忆子进程失败")
        else:
            reason = (result.stderr or result.stdout or "记忆子进程失败").strip()[-1000:]
    except subprocess.TimeoutExpired:
        reason = "记忆 venv 子进程超过 25 秒"
    except (OSError, ValueError, IndexError) as exc:
        reason = "记忆 venv 子进程失败：%s" % exc
    return _memory_keyword_fallback(project, task, prompt, st or {}, cfg, reason)


def hermes_ephemeral_system_prompt(project, task, st):
    """Build the non-persistent task summary Hermes appends every API call."""
    cfg = config_for_state(project, st)
    brief = read_text(os.path.join(project.task_dir(task), "brief.md"))
    scope = st.get("scope") or parse_scope(brief, required=False) or {}
    amendments = []
    for amendment in st.get("amendments") or []:
        if isinstance(amendment, dict):
            amendments.append(str(amendment.get("id", "")))
    lines = [
        "Baton task charter summary (authoritative task context; not new user input):",
        f"task={task}", f"goal={brief[:4000]}",
        "allow=" + json.dumps(scope.get("allow", []), ensure_ascii=False),
        "deny=" + json.dumps(scope.get("deny", []), ensure_ascii=False),
        "acceptance=" + json.dumps(scope.get("acceptance", []), ensure_ascii=False),
        "frozen=" + json.dumps(scope.get("frozen", []), ensure_ascii=False),
        "verify=" + str(scope.get("verify", "none")),
        "amendments=" + ",".join(x for x in amendments if x) if amendments else "amendments=none",
        "Project settings:", f"language={cfg.get('language', '中文')}",
        f"major_files={cfg['change_size']['major_files']}",
        f"major_lines={cfg['change_size']['major_lines']}",
        f"test_command={cfg['supervision'].get('test_command') or 'brief-defined'}",
        f"baton_dir={project.dir}",
        "Do not alter the charter; report facts and follow the SOUL.md output contract.",
    ]
    leg = current_leg(st) if isinstance(st, dict) else None
    core = (leg or {}).get("memory_core", "") if isinstance(leg, dict) else ""
    if core:
        lines.extend(["Project pinned memory (reference data, below charter and current instruction):", core])
    return "\n".join(lines)


WINDOWS_HERMES_ERROR = ("Hermes executor is unsupported on Windows: Hermes + sandbox-exec are macOS-oriented. "
                        "Set executor.backend to codex.")


def require_supported_executor(cfg):
    if os.name == "nt" and cfg["executor"].get("backend") == "hermes":
        raise ConfigError(WINDOWS_HERMES_ERROR)


def hermes_preflight(project, cfg):
    require_supported_executor(cfg)
    """Reject unsafe Hermes profile state before starting a leg."""
    ex = cfg["executor"]
    profile = hermes_profile_dir(cfg)
    env_file = os.path.join(profile, ".env")
    if hermes_env_has_assignments(env_file):
        raise ConfigError(f"Hermes profile .env 非空，拒绝启动：{env_file}")
    entries = _profile_credential_entries(os.path.join(profile, "auth.json"))
    if len(entries) != 1:
        raise ConfigError(f"Hermes profile auth.json 必须恰好有一条 openai-codex 凭据，当前 {len(entries)} 条；请登录或删除多余条目")
    account = hermes_account_ids(cfg)
    if account["mismatch"]:
        raise ConfigError("Hermes 与 Codex CLI 的账号 id 不一致，拒绝启动；请登录同一个账号")
    if ex.get("network_access"):
        command = [hermes_executable(cfg), "-p", ex.get("hermes_profile", "baton-exec-v2"), "tools", "list"]
        result = subprocess.run(command, env=_hermes_subprocess_env(cfg), capture_output=True, text=True, encoding="utf-8")
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode != 0:
            print("WARN：无法确认 Hermes web_search 工具列表；按本机 CLI 版本跳过检查。")
        elif "web_search" not in output:
            raise ConfigError("network_access=true 但 Hermes 工具列表没有 web_search；请先配置搜索后端")


def memory_mcp_ready_path(project, task, leg_n):
    return project.leg_path(task, leg_n, "mcp-ready")


def _mcp_jsonrpc_request(proc, method, params, ident, timeout=4.0):
    request = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
    proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
    proc.stdin.flush()
    deadline = now() + timeout
    while now() < deadline:
        remaining = max(0.05, deadline - now())
        if os.name == "nt":
            ready = platform_support.pipe_ready(proc.stdout.fileno(), remaining)
        else:
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("id") == ident:
            return value
    raise ConfigError("baton-memory %s 超时" % method)


def memory_mcp_preflight(project, task, leg_n, cfg, timeout=5.0):
    """Start the exact stdio server command and verify its two read-only tools."""
    python = memory_venv_python(cfg)
    if not python:
        return False, "本轮没有记忆工具（venv 不存在）"
    env = os.environ.copy()
    env.update({"MEM0_TELEMETRY": "false", "BATON_HOME": memory_baton_home(cfg),
                "BATON_TASK": str(task), "BATON_LEG": str(leg_n),
                "BATON_LEG_STARTED": str(now()), "BATON_MCP_PREFLIGHT": "1"})
    proc = subprocess.Popen([python, "-m", "baton_memory.server"], cwd=project.root,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, encoding="utf-8")
    try:
        if proc.stdin is None or proc.stdout is None:
            raise ConfigError("baton-memory preflight 没有 stdio")
        _mcp_jsonrpc_request(proc, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                    "clientInfo": {"name": "baton-preflight", "version": "1"}}, 1, timeout)
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        response = _mcp_jsonrpc_request(proc, "tools/list", {}, 2, timeout)
        names = {item.get("name") for item in response.get("result", {}).get("tools", [])}
        if names != {"memory_search", "archive_search"}:
            raise ConfigError("baton-memory 工具列表不符：%s" % sorted(names))
        return True, ""
    finally:
        if proc.stdin:
            proc.stdin.close()
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()


def memory_mcp_status(project, task, leg_n, started_at):
    path = memory_mcp_ready_path(project, task, leg_n)
    value = read_json(path, {})
    if not isinstance(value, dict) or not value.get("time"):
        return False, "本轮没有记忆工具：mcp-ready 标记缺失"
    try:
        ready_at = float(value["time"])
        started = float(started_at)
    except (TypeError, ValueError):
        return False, "本轮没有记忆工具：mcp-ready 时间戳无效"
    if ready_at < started or ready_at > started + 60:
        return False, "本轮没有记忆工具：mcp-ready 时间晚于开轮 60 秒或早于开轮"
    return True, ""


def hermes_session_id(value):
    """Return the last Hermes `session_id: ...` marker from stderr text/path."""
    text = read_text(value) if isinstance(value, str) and os.path.isfile(value) else str(value or "")
    matches = re.findall(r"(?:^|\n)\s*session_id:\s*([^\s]+)\s*$", text, re.MULTILINE)
    return matches[-1] if matches else None


def hermes_error_text(stdout, stderr):
    lines = []
    for text in (stdout or "", stderr or ""):
        lines.extend(line.strip() for line in text.splitlines() if "Error:" in line)
    return (lines[-1] if lines else ((stderr or stdout or "").strip().splitlines() or ["Hermes 进程失败"])[-1])[:2000]


def parse_hermes_result(stdout, stderr="", returncode=0):
    """Extract and validate the final BATON_RESULT pair from Hermes stdout."""
    if returncode != 0:
        return None, hermes_error_text(stdout, stderr)
    matches = re.findall(r"BATON_RESULT_BEGIN\s*\n(.*?)\nBATON_RESULT_END", stdout or "", re.DOTALL)
    if not matches:
        return None, "stdout 中没有完整的 BATON_RESULT_BEGIN/BATON_RESULT_END 标记"
    try:
        value = json.loads(matches[-1].strip())
    except (TypeError, ValueError) as exc:
        return None, f"BATON_RESULT JSON 无法解析：{exc}"
    errors = validate_executor_result(value, require_memory=True)
    return (value, "") if not errors else (None, "；".join(errors))


# ---------------------------------------------------------------- project statusline

def settings_local_path(project):
    return os.path.join(project.root, ".claude", "settings.local.json")


def skills_dir():
    return os.environ.get("CLAUDE_SKILLS_DIR") or os.path.join(
        os.path.dirname(baton_statusline.user_settings_path()), "skills")


def statusline_script_path():
    """Prefer the installed skills/baton link, which survives moving the clone."""
    ours = os.path.join(HERE, "statusline.py")
    link = os.path.join(skills_dir(), "baton", "scripts", "statusline.py")
    try:
        if os.path.exists(link) and os.path.samefile(link, ours):
            return link
    except OSError:
        pass
    return ours


def statusline_entry(project, previous):
    """The statusLine object Baton installs; refreshInterval follows the statusline it wraps."""
    interval = previous.get("refreshInterval") if isinstance(previous, dict) else None
    for path in (os.path.join(project.root, ".claude", "settings.json"), baton_statusline.user_settings_path()):
        if interval:
            break
        sl = (read_json(path, {}) or {}).get("statusLine") if isinstance(read_json(path, {}), dict) else None
        if isinstance(sl, dict) and not baton_statusline.is_baton_command(sl.get("command")):
            interval = sl.get("refreshInterval")
    command = f"python3 {shlex.quote(statusline_script_path())} {baton_statusline.MARK}"
    if os.name == "nt":
        # Claude Code uses Git Bash on Windows; slash paths survive shell parsing.
        command = (f"{shlex.quote(sys.executable.replace(chr(92), '/'))} "
                   f"{shlex.quote(statusline_script_path().replace(chr(92), '/'))} "
                   f"{baton_statusline.MARK}")
    return {"type": "command", "command": command, "refreshInterval": interval or 60}


def statusline_script_ok(command):
    """The script named in a Baton statusLine command still exists."""
    try:
        parts = shlex.split(command or "")
    except ValueError:
        return False
    return len(parts) >= 2 and os.path.exists(parts[1])


def settings_rel_top(project):
    """Path of the project's settings.local.json relative to the git top level."""
    prefix = git(project.root, "rev-parse", "--show-prefix", check=False).rstrip("\n")
    return prefix + ".claude/settings.local.json"


def gitignore_escape(rel):
    out = re.sub(r"([\\*?\[])", r"\\\1", rel)
    if out.endswith(" "):
        out = out[:-1] + "\\ "
    return out


def settings_tracked(project):
    if not is_git(project.root):
        return False
    top = git_top(project.root)
    return subprocess.run([platform_support.executable("git"), "-C", top, "ls-files", "--error-unmatch", "--",
                           ":(icase)" + settings_rel_top(project)], capture_output=True).returncode == 0


def settings_ignored(project):
    top = git_top(project.root)
    return subprocess.run([platform_support.executable("git"), "-C", top, "check-ignore", "-q", "--", settings_rel_top(project)]).returncode == 0


def ensure_settings_ignored(project):
    """Keep the machine-local settings file out of git without touching the project's .gitignore.

    Returns a warning string when the file still is not ignored, else None.
    """
    if not is_git(project.root):
        return None
    if settings_tracked(project):
        return "注意：.claude/settings.local.json 已被 git 跟踪，里面的本机路径会被提交"
    if settings_ignored(project):
        return None
    top = git_top(project.root)
    pattern = "/" + gitignore_escape(settings_rel_top(project))
    path = git(top, "rev-parse", "--git-path", "info/exclude").strip()
    path = path if os.path.isabs(path) else os.path.join(top, path)
    if pattern not in read_text(path).splitlines():
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n# Baton: machine-local Claude Code settings\n{pattern}\n")
    if not settings_ignored(project):
        return "注意：.claude/settings.local.json 没能被 git 忽略（项目规则又把它放行了），提交前请检查"
    return None


def baton_statusline_installed(project):
    data = read_json(settings_local_path(project))
    current = data.get("statusLine") if isinstance(data, dict) else None
    return isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))


def statusline_install(project, force=False):
    """Point the project's statusLine at Baton's wrapper; the previous one stays as line 1."""
    path = settings_local_path(project)
    root_real = os.path.realpath(project.root)
    real = os.path.realpath(path)
    if not force and not (real == root_real or real.startswith(root_real + os.sep)):
        return f"未改 statusline：{path} 是指向项目外的符号链接（{real}）。确实要装就运行 baton statusline install --force"
    if settings_tracked(project) and not force:
        return (f"未改 statusline：{path} 已被 git 跟踪，写入本机路径会被提交。"
                f"确实要装就运行 baton statusline install --force")
    created = not os.path.lexists(path)
    data = {}
    if not created:
        data = read_json(path)
        if not isinstance(data, dict):
            return f"未配置 statusline：{path} 读不出合法的 JSON 对象（文件损坏或是失效的符号链接），请手动检查"
    current = data.get("statusLine")
    if isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command")):
        fresh = statusline_entry(project, current)
        if current.get("command") != fresh["command"]:
            current["command"] = fresh["command"]  # skill moved: refresh the script path
            try:
                write_json_keep(path, data)
            except OSError as e:
                return f"未能更新 statusline：{e}"
        warn = ensure_settings_ignored(project)
        return f"statusline 已是 Baton 版本（{path}）" + (f"\n{warn}" if warn else "")
    data["statusLine"] = statusline_entry(project, current)
    claude_dir_existed = os.path.isdir(os.path.dirname(path))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_json_keep(path, data)
    except OSError as e:
        return f"未配置 statusline：写入 {path} 失败（{e}）"
    os.makedirs(project.dir, exist_ok=True)
    write_json(project.p("statusline-saved.json"),
               {"created_file": created, "had_statusline": current is not None, "previous": current})
    warn = ensure_settings_ignored(project)
    # Claude Code hot-reloads settings files, but only picks up a new file mid-session
    # if its folder already existed when the session started.
    when = ("如果会话启动时 .claude/ 已存在，当前会话会自动加载，否则重开会话后生效" if claude_dir_existed
            else "本次新建了 .claude/ 目录，需要重开 Claude Code 会话后生效")
    return (f"statusline 已配置：{path}\n原有 statusline 保留为第一行，第二行显示 Codex 额度；{when}。"
            f"撤销用 baton statusline uninstall" + (f"\n{warn}" if warn else ""))


def statusline_uninstall(project):
    path = settings_local_path(project)
    data = read_json(path)
    current = data.get("statusLine") if isinstance(data, dict) else None
    if not (isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))):
        return "当前项目的 statusline 不是 Baton 配置的，未改动"
    saved = read_json(project.p("statusline-saved.json"), {}) or {}
    if saved.get("had_statusline") and saved.get("previous") is not None:
        data["statusLine"] = saved["previous"]
    else:
        data.pop("statusLine", None)
    try:
        if not data and saved.get("created_file") and not os.path.islink(path):
            os.remove(path)
        else:
            write_json_keep(path, data)
    except OSError as e:
        return f"撤销失败：{e}"
    try:
        os.remove(project.p("statusline-saved.json"))
    except OSError:
        pass
    return f"已撤销 Baton statusline（{path}）"


def statusline_report(project):
    data = read_json(settings_local_path(project))
    current = data.get("statusLine") if isinstance(data, dict) else None
    installed = isinstance(current, dict) and baton_statusline.is_baton_command(current.get("command"))
    base = baton_statusline.resolve_base(project.root)
    state = "未安装"
    if installed:
        state = "已安装 Baton 版本" if statusline_script_ok(current.get("command")) \
            else "已安装，但命令里的脚本不存在（Baton 被移走了？重新 baton statusline install）"
    return (f"statusline：{state}（{settings_local_path(project)}）\n"
            f"第一行来源：{base or '无'}")


# ---------------------------------------------------------------- commands

def write_baton_gitignore(project):
    gi = project.p(".gitignore")
    have = read_text(gi).splitlines() if os.path.exists(gi) else None
    if have is None:
        os.makedirs(project.dir, exist_ok=True)
        with open(gi, "w", encoding="utf-8") as f:
            f.write("# Baton runtime and machine-local files\n" + "\n".join(BATON_GITIGNORE) + "\n")
        return
    missing = [line for line in BATON_GITIGNORE if line not in have]
    if missing:
        with open(gi, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(missing) + "\n")


def _rebase_paths(obj, old_root, new_root):
    """Rewrite absolute paths under old_root (as stored in task state) to new_root."""
    if isinstance(obj, str):
        if obj == old_root or obj.startswith(old_root + os.sep):
            return new_root + obj[len(old_root):]
        return obj
    if isinstance(obj, list):
        return [_rebase_paths(x, old_root, new_root) for x in obj]
    if isinstance(obj, dict):
        return {k: _rebase_paths(v, old_root, new_root) for k, v in obj.items()}
    return obj


def relink_state(project, old_id):
    """After the project folder moved: move its old state dir to the new id and fix stored paths."""
    old_dir = os.path.join(os.path.dirname(project.state_dir), old_id)
    if not os.path.isdir(old_dir):
        die(f"找不到原来的状态目录 {old_dir}；如果这是复制项目或换了机器，请用 baton init --fresh")
    if os.path.exists(project.state_dir):
        die(f"新的状态目录 {project.state_dir} 已经存在，不覆盖；先确认里面没有要保留的东西，移走后再试")
    id_data = read_json(project.id_file) or {}
    old_root = id_data.get("root")
    if old_root and os.path.isdir(old_root):
        original_id = read_json(os.path.join(old_root, ".baton", "project-id")) or {}
        if original_id.get("id") == old_id:
            die(f"检测到这是复制项目：原目录 {old_root} 仍存在且 project-id 仍为 {old_id}；"
                "为避免搬走原项目状态，请改用 baton init --fresh")
    os.rename(old_dir, project.state_dir)
    if old_root and old_root != project.root:
        tasks_dir = os.path.join(project.state_dir, "tasks")
        for task in (os.listdir(tasks_dir) if os.path.isdir(tasks_dir) else []):
            path = os.path.join(tasks_dir, task, "state.json")
            st = read_json(path)
            if isinstance(st, dict):
                write_json(path, _rebase_paths(st, old_root, project.root))
    project.record_id()
    print(f"已把 {old_dir} 接到 {project.state_dir}")


def hermes_home(cfg=None):
    """Resolve the Hermes home without touching the user's real home in tests."""
    executor = ((cfg or {}).get("executor") or {}) if isinstance(cfg, dict) else {}
    return os.path.abspath(os.path.expanduser(
        os.environ.get("HERMES_HOME") or executor.get("hermes_home") or "~/.hermes"))


def hermes_profile_dir(cfg):
    profile = ((cfg.get("executor") or {}).get("hermes_profile") or "baton-exec-v2")
    return os.path.join(hermes_home(cfg), "profiles", profile)


def memory_baton_home(cfg=None):
    """Resolve BATON_HOME without consulting the user's real home in tests."""
    explicit = os.environ.get("BATON_HOME")
    if explicit and "${" not in explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    project = (cfg or {}).get("project") if isinstance(cfg, dict) else None
    state = project.get("state_dir") if isinstance(project, dict) else None
    if state:
        return os.path.dirname(os.path.dirname(os.path.abspath(state)))
    return os.path.abspath(os.path.expanduser("~/.baton"))


def memory_venv_python(cfg=None):
    path = platform_support.venv_python(memory_baton_home(cfg))
    return path if os.path.isfile(path) else None


def memory_installed(cfg=None):
    return bool(memory_venv_python(cfg))


def hermes_python(cfg):
    executor = (cfg.get("executor") or {}) if isinstance(cfg, dict) else {}
    explicit = os.environ.get("HERMES_PYTHON") or executor.get("hermes_python")
    agent = os.environ.get("HERMES_AGENT_DIR") or executor.get("hermes_agent_dir")
    candidates = [explicit] if explicit else []
    if agent:
        candidates.append(os.path.join(os.path.expanduser(agent), "venv", "bin", "python"))
    candidates.append(os.path.join(hermes_home(cfg), "hermes-agent", "venv", "bin", "python"))
    return next((os.path.abspath(os.path.expanduser(path)) for path in candidates if path and os.path.isfile(
        os.path.expanduser(path))), None)


HERMES_DEFAULT_SOUL_MARKER = "You are Hermes Agent, an intelligent AI assistant created by Nous Research"


def hermes_default_soul(cfg):
    """Read Hermes' built-in SOUL.md, with a marker fallback for older installs."""
    python = hermes_python(cfg)
    if python:
        script = ("from hermes_cli.default_soul import DEFAULT_SOUL_MD; "
                  "import sys; sys.stdout.write(DEFAULT_SOUL_MD)")
        env = _hermes_subprocess_env(cfg)
        agent = os.environ.get("HERMES_AGENT_DIR") or (cfg.get("executor") or {}).get("hermes_agent_dir")
        if agent:
            env["PYTHONPATH"] = os.path.expanduser(agent) + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run([python, "-c", script], env=env, capture_output=True, text=True, encoding="utf-8")
        if result.returncode == 0 and (result.stdout or "").strip():
            return result.stdout
    return HERMES_DEFAULT_SOUL_MARKER


def _soul_matches_hermes_default(current, cfg):
    expected = hermes_default_soul(cfg)
    if expected == HERMES_DEFAULT_SOUL_MARKER:
        return current.startswith(HERMES_DEFAULT_SOUL_MARKER)
    return current == expected


def hermes_env_has_assignments(path):
    """Return whether a profile .env contains an effective assignment line."""
    for line in read_text(path).splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return True
    return False


def hermes_executable(cfg):
    explicit = os.environ.get("HERMES_BIN") or ((cfg.get("executor") or {}).get("hermes_bin") if isinstance(cfg, dict) else None)
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit)) if os.path.sep in explicit else explicit
    path = shutil.which("hermes")
    return path or "hermes"


def hermes_profile_auth_path(cfg):
    return os.path.join(hermes_profile_dir(cfg), "auth.json")


def _sha256_file(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def hermes_root_auth_path(cfg):
    return os.path.join(hermes_home(cfg), "auth.json")


def _hermes_subprocess_env(cfg):
    env = os.environ.copy()
    env["HERMES_HOME"] = hermes_home(cfg)
    return env


def _hermes_profile_create(cfg):
    profile = (cfg.get("executor") or {}).get("hermes_profile", "baton-exec-v2")
    profile_dir = hermes_profile_dir(cfg)
    if os.path.isdir(profile_dir):
        return False
    command = [hermes_executable(cfg), "profile", "create", profile, "--no-skills", "--no-alias"]
    result = subprocess.run(command, env=_hermes_subprocess_env(cfg), capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise ConfigError("Hermes profile 创建失败：" + (result.stderr or result.stdout or "退出码 %s" % result.returncode).strip()[-1000:])
    os.makedirs(profile_dir, exist_ok=True)
    return True


def hermes_config_text(cfg):
    # Keep this fixed and minimal: per-project settings are command-line/env state.
    provider = ((cfg.get("executor") or {}).get("provider") or "openai-codex")
    model = "gpt-5.6-luna"
    text = (f"model: {{provider: {provider}, default: {model}}}\n"
            "memory: {memory_enabled: false, user_profile_enabled: false, provider: ''}\n"
            'tools: {tool_search: {enabled: "off"}}\n'
            "display: {show_reasoning: false, streaming: false, inline_diffs: false}\n")
    if memory_installed(cfg):
        python = memory_venv_python(cfg) or "~/.baton/venv/bin/python"
        text += ("mcp_single_query_discovery_timeout: 60\n"
                 "mcp_servers:\n"
                 "  baton-memory:\n"
                 "    command: %s\n"
                 '    args: ["-m", "baton_memory.server"]\n'
                 '    env: {MEM0_TELEMETRY: "false", BATON_HOME: "${BATON_HOME}", BATON_TASK: "${BATON_TASK}", BATON_LEG: "${BATON_LEG}", BATON_LEG_STARTED: "${BATON_LEG_STARTED}"}\n'
                 "    timeout: 30\n"
                 "    tools: {resources: false, prompts: false}\n" % python)
    return text


def hermes_soul_text():
    template = read_text(os.path.join(TEMPLATES, "hermes-SOUL.md"))
    schema = read_text(os.path.join(TEMPLATES, "leg-result.schema.json")).strip()
    return template.replace("{{LEG_RESULT_SCHEMA}}", schema)


def _scan_soul_with_hermes(cfg, text):
    """Run Hermes' own threat scanner before any SOUL.md write."""
    python = hermes_python(cfg)
    if not python:
        raise ConfigError("找不到 Hermes venv Python，拒绝写入 SOUL.md；请设置 HERMES_PYTHON 或 HERMES_AGENT_DIR")
    script = ("import json,sys; from tools.threat_patterns import scan_for_threats; "
              "print(json.dumps(scan_for_threats(sys.stdin.read(), 'context')))" )
    env = _hermes_subprocess_env(cfg)
    agent = os.environ.get("HERMES_AGENT_DIR") or (cfg.get("executor") or {}).get("hermes_agent_dir")
    if agent:
        env["PYTHONPATH"] = os.path.expanduser(agent) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([python, "-c", script], input=text, capture_output=True, text=True, env=env, encoding="utf-8")
    if result.returncode != 0:
        raise ConfigError("Hermes 注入扫描失败：" + (result.stderr or result.stdout or "退出码 %s" % result.returncode).strip()[-1000:])
    findings = None
    for line in reversed((result.stdout or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, list):
            findings = value
            break
    if findings is None:
        raise ConfigError("Hermes 注入扫描没有返回 JSON 结果")
    return [str(item) for item in findings]


def _profile_upgrade_diff(path, before, after):
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                         fromfile=path + " (before)", tofile=path + " (after)"))
    if diff:
        print(diff.rstrip())


def _write_readonly_profile_file(path, content, cfg, scan=False, upgrade=False):
    if os.path.exists(path):
        current = read_text(path)
        if current != content:
            if not upgrade:
                print(f"WARN：Hermes profile 文件已有内容且不同，未覆盖：{path}")
            else:
                _profile_upgrade_diff(path, current, content)
                os.chmod(path, 0o644)
                with open(path, "w", encoding="utf-8") as stream:
                    stream.write(content)
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        return False
    if scan:
        findings = _scan_soul_with_hermes(cfg, content)
        if findings:
            raise ConfigError("SOUL.md 注入扫描命中，拒绝写入：" + ", ".join(findings))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(path, 0o444)
    return True


def _write_hermes_soul(path, content, cfg, created=False, upgrade=False):
    """Replace Hermes' generated default SOUL, preserving user-owned files."""
    exists = os.path.exists(path)
    current = read_text(path) if exists else ""
    if exists and current == content:
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass
        return False
    if exists and current and not created and not _soul_matches_hermes_default(current, cfg):
        if not upgrade:
            print(f"WARN：Hermes profile 文件已有内容且不同，未覆盖：{path}")
            return False
        _profile_upgrade_diff(path, current, content)
    findings = _scan_soul_with_hermes(cfg, content)
    if findings:
        raise ConfigError("SOUL.md 注入扫描命中，拒绝写入：" + ", ".join(findings))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if exists:
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(content)
    os.chmod(path, 0o444)
    return True


def _context_candidate(directory, names):
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def hermes_context_files(project):
    """List project context in Hermes' precedence order.

    Hermes versions have moved this helper, so Baton keeps the documented
    fallback here: a .hermes/HERMES file wins; otherwise AGENTS files merge from
    git root to cwd; only if none exist do cwd-level CLAUDE/cursorrules apply.
    """
    root = git_repo_top(project.root) or project.root
    cwd = project.root
    current = canonical_dir(root)
    target = canonical_dir(cwd)
    hermes_candidates = []
    probe = target
    while True:
        candidate = _context_candidate(probe, (".hermes.md", "HERMES.md"))
        if candidate:
            hermes_candidates.append(candidate)
        if probe == current or not probe.startswith(current + os.sep):
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    if hermes_candidates:
        return [hermes_candidates[0]]
    chain = []
    current = canonical_dir(root)
    while True:
        path = os.path.join(current, "AGENTS.md")
        if os.path.isfile(path):
            chain.append(path)
        if current == target:
            break
        parent = os.path.dirname(current)
        if parent == current or not target.startswith(current + os.sep):
            break
        current = parent
    if chain:
        return chain
    for name in ("CLAUDE.md", ".cursorrules"):
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            return [path]
    return []


def legacy_claude_instruction_warnings(project):
    """Find v1 Baton wording in user/project Claude instructions.

    These files remain user-owned and are only read by doctor.  The warning is
    deliberately non-blocking so an existing v1 project can be migrated in a
    separate step.
    """
    paths = []
    user_path = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    if os.path.isfile(user_path):
        paths.append(user_path)
    root = git_repo_top(project.root) or project.root
    current = canonical_dir(root)
    target = canonical_dir(project.root)
    chain = []
    while True:
        path = os.path.join(current, "CLAUDE.md")
        if os.path.isfile(path):
            chain.append(path)
        if current == target or not target.startswith(current + os.sep):
            break
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    paths.extend(reversed(chain))
    patterns = (
        re.compile(r"judge\.model", re.IGNORECASE),
        re.compile(r"子[ 　]*agent.{0,60}(裁判|审阅|审代码)", re.IGNORECASE | re.DOTALL),
        re.compile(r"(裁判|审阅|审代码).{0,60}子[ 　]*agent", re.IGNORECASE | re.DOTALL),
    )
    warnings = []
    seen = set()
    for path in paths:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        text = read_text(path)
        if any(pattern.search(text) for pattern in patterns):
            warnings.append(path)
    return warnings


def _profile_credential_entries(path):
    raw = read_json(path, None)
    if raw is None:
        return []
    found = []
    seen = set()

    def add(value):
        if isinstance(value, dict):
            marker = id(value)
            if marker not in seen:
                seen.add(marker)
                found.append(value)

    def visit(value, parent_key=""):
        if isinstance(value, dict):
            for key, item in value.items():
                key_text = str(key).lower().replace("_", "-")
                if key_text == "openai-codex":
                    if isinstance(item, list):
                        for entry in item:
                            add(entry)
                    else:
                        add(item)
                if isinstance(item, dict):
                    provider = str(item.get("provider", item.get("backend", ""))).lower()
                    kind = str(item.get("type", "")).lower()
                    if provider == "openai-codex" or (parent_key == "openai-codex" and kind in {"oauth", "codex"}):
                        add(item)
                    visit(item, key_text)
                elif isinstance(item, list):
                    visit(item, key_text)
        elif isinstance(value, list):
            for item in value:
                visit(item, parent_key)
                if parent_key == "openai-codex":
                    add(item)
    visit(raw)
    return found


def _credential_identity(entry):
    if not isinstance(entry, dict):
        return ""
    for key in ("account_id", "accountId", "chatgpt_account_id", "user_id", "userId", "email", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    for value in entry.values():
        if isinstance(value, dict):
            result = _credential_identity(value)
            if result:
                return result
    return ""


def _account_id_hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest() if value else None


def _jwt_payload(token):
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        encoded = token.split(".", 2)[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(__import__("base64").urlsafe_b64decode(encoded).decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _jwt_account_id(token):
    payload = _jwt_payload(token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and isinstance(auth.get("chatgpt_account_id"), str):
        return auth["chatgpt_account_id"]
    for key in ("chatgpt_account_id", "account_id"):
        if isinstance(payload.get(key), str) and payload[key]:
            return payload[key]
    return ""


def _token_account_id(entry):
    if not isinstance(entry, dict):
        return "", False
    tokens = entry.get("tokens") if isinstance(entry.get("tokens"), dict) else entry
    access = tokens.get("access_token")
    identity = _jwt_account_id(access)
    if not identity:
        identity = _jwt_account_id(tokens.get("id_token"))
    direct = tokens.get("account_id")
    conflict = bool(direct and identity and direct != identity)
    return identity or (direct if isinstance(direct, str) else ""), conflict


def _codex_auth_identity(path):
    raw = read_json(path, {}) or {}
    if not isinstance(raw, dict):
        return "", False
    return _token_account_id(raw.get("tokens") if isinstance(raw.get("tokens"), dict) else raw)


def _hermes_auth_identity(path):
    raw = read_json(path, {}) or {}
    pool = raw.get("credential_pool") if isinstance(raw, dict) else None
    entries = pool.get("openai-codex") if isinstance(pool, dict) else None
    if isinstance(entries, dict):
        entries = [entries]
    if not isinstance(entries, list):
        return "", False
    for entry in entries:
        identity, conflict = _token_account_id(entry)
        if identity or conflict:
            return identity, conflict
    return "", False


def hermes_account_ids(cfg):
    """Resolve Codex/Hermes account ids from JWT payloads without exposing tokens."""
    profile_id, profile_conflict = _hermes_auth_identity(hermes_profile_auth_path(cfg))
    codex_home = os.environ.get("CODEX_HOME") or "~/.codex"
    codex_id, codex_conflict = _codex_auth_identity(os.path.join(os.path.expanduser(codex_home), "auth.json"))
    return {"profile": profile_id, "codex": codex_id,
            "mismatch": profile_conflict or codex_conflict or bool(profile_id and codex_id and profile_id != codex_id)}


def _codex_account_id(path):
    return _codex_auth_identity(path)[0]


def prepare_hermes_profile(project, cfg, upgrade_profile=False):
    created = _hermes_profile_create(cfg)
    profile = hermes_profile_dir(cfg)
    _write_hermes_soul(os.path.join(profile, "SOUL.md"), hermes_soul_text(), cfg,
                        created=created, upgrade=upgrade_profile)
    _write_readonly_profile_file(os.path.join(profile, "config.yaml"), hermes_config_text(cfg), cfg,
                                 upgrade=upgrade_profile)
    root_auth = hermes_root_auth_path(cfg)
    before = _sha256_file(root_auth)
    baseline_path = os.path.join(project.state_dir, "hermes-root-auth.sha256")
    if not os.path.exists(baseline_path):
        os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
        with open(baseline_path, "w", encoding="utf-8") as stream:
            stream.write((before or "missing") + "\n")
    profile_name = (cfg.get("executor") or {}).get("hermes_profile", "baton-exec-v2")
    print(f"Hermes profile：{profile}（{'新建' if created else '已存在'}）")
    print("请登录与 Codex CLI 相同的账号：")
    print(f"  hermes -p {profile_name} auth add openai-codex --type oauth")
    print(f"  hermes -p {profile_name} auth list openai-codex")
    print(f"  hermes -p {profile_name} auth remove openai-codex <从根目录带进来的条目>")
    after = _sha256_file(root_auth)
    if before != after:
        print("WARN：Hermes 根目录 auth.json 在 init 期间发生变化；请检查账号池。")
    print("Hermes 实际会加载的项目上下文文件：")
    for path in hermes_context_files(project):
        print(f"  {path}")
    if not hermes_context_files(project):
        print("  （无）")


def migrate_baton_config(project):
    """Migrate a v1 project config in place, preserving a byte-for-byte backup."""
    path = project.baton_config_path
    if not os.path.isfile(path):
        die(f"找不到要迁移的项目配置：{path}")
    try:
        with open(path, encoding="utf-8") as stream:
            original_text = stream.read()
            original = json.loads(original_text)
    except (OSError, json.JSONDecodeError) as exc:
        die(f"无法迁移 {path}：{exc}")
    if not isinstance(original, dict):
        die(f"无法迁移 {path}：顶层必须是 JSON object")
    migrated = json.loads(json.dumps(original))
    if "conductor" not in migrated and isinstance(migrated.get("judge"), dict):
        migrated["conductor"] = dict(migrated["judge"])
    migrated.pop("judge", None)
    executor = migrated.setdefault("executor", {})
    if not isinstance(executor, dict):
        die("无法迁移：executor 必须是 JSON object")
    executor["backend"] = "codex" if os.name == "nt" else "hermes"
    executor["model"] = "gpt-5.6-luna"
    if os.name != "nt":
        for key in ("service_tier", "disable_fast_mode", "sandbox"):
            executor.pop(key, None)
    new_text = json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
    if new_text == original_text:
        print(f"配置已经是 v2，无需迁移：{path}")
        return False
    backup = path + ".v1.bak"
    if not os.path.exists(backup):
        with open(backup, "w", encoding="utf-8") as stream:
            stream.write(original_text)
        print(f"已备份 v1 配置：{backup}")
    else:
        print(f"保留已有 v1 备份：{backup}")
    print("配置迁移差异：")
    for line in difflib.unified_diff(original_text.splitlines(), new_text.splitlines(),
                                     fromfile=path, tofile=path + " (v2)", lineterm=""):
        print(line)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(new_text)
    return True


def cmd_init(project, args):
    try:
        validate_external_state(project)
    except RuntimeError as e:
        die(str(e))
    top = git_repo_top(project.root)
    if top and project.root != top:
        print(f"项目目录：{project.root}")
        print(f"git 顶层：{top}")
        if not getattr(args, "accept_boundary", False):
            die("项目目录与 git 顶层不同；确认边界后加 --accept-boundary 重试（未创建任何文件）")
    old_id = project.recorded_id()
    if old_id and old_id != project.project_id:
        relink = getattr(args, "relink", False)
        fresh = getattr(args, "fresh", False)
        if relink and fresh:
            die("--relink 和 --fresh 不能同时使用")
        if fresh:
            if os.path.exists(project.state_dir) and os.listdir(project.state_dir):
                die(f"新的状态目录 {project.state_dir} 已经存在，不覆盖；请先确认后再用 --fresh")
            print(f"复制项目将使用新的项目外状态目录：{project.state_dir}（原状态保留在旧项目）")
        elif not relink:
            project.check_identity()  # explains the move and exits
        else:
            relink_state(project, old_id)
    if getattr(args, "migrate", False):
        migrate_baton_config(project)
    # Validate an existing baton.json before changing the project state.  When
    # it is absent, the skill defaults and any existing .baton override form
    # the values used for the generated file below.
    require_supported_executor(project.config)
    for d in ("tasks", "reports", "reviews", "decisions", "runs"):
        os.makedirs(project.p(d), exist_ok=True)
    project = Project(project.root)  # the newly created directory now has its on-disk spelling
    os.makedirs(project.state_dir, exist_ok=True)
    write_baton_gitignore(project)
    if not os.path.exists(project.p("config.json")):
        write_json(project.p("config.json"), {
            "_comment": "Project overrides for Baton. Put model and test settings in <project>/baton.json; quota.* is ignored in project files.",
        })
    if not os.path.exists(project.baton_config_path):
        cfg = project.config
        write_json(project.baton_config_path, {
            "conductor": {
                "model": cfg["conductor"]["model"],
                "council_for_major_decisions": cfg["conductor"]["council_for_major_decisions"],
            },
            "monitor": {
                "model": cfg["monitor"]["model"],
                "reasoning_effort": cfg["monitor"].get("reasoning_effort"),
                "final_reasoning_effort": cfg["monitor"].get("final_reasoning_effort"),
                "timeout_minutes": cfg["monitor"].get("timeout_minutes"),
            },
            "executor": {
                "backend": cfg["executor"]["backend"],
                "hermes_profile": cfg["executor"].get("hermes_profile", "baton-exec-v2"),
                "provider": cfg["executor"].get("provider", "openai-codex"),
                "toolsets": cfg["executor"].get("toolsets", ["file", "terminal", "todo"]),
                "sandbox_exec": cfg["executor"].get("sandbox_exec", True),
                "model": cfg["executor"]["model"],
                "reasoning_effort": cfg["executor"]["reasoning_effort"],
                "escalation": cfg["executor"].get("escalation", {"enabled": False}),
                "service_tier": cfg["executor"]["service_tier"],
                "disable_fast_mode": cfg["executor"].get("disable_fast_mode", True),
                "network_access": cfg["executor"].get("network_access", False),
            },
            "memory": {
                "backend": cfg.get("memory", {}).get("backend", "mem0"),
                "embedder": cfg.get("memory", {}).get("embedder", {}),
                "inject_top_k": cfg.get("memory", {}).get("inject_top_k", 10),
                "core_max_chars": cfg.get("memory", {}).get("core_max_chars", 2200),
                "curation_model": cfg.get("memory", {}).get("curation_model", "sonnet"),
            },
            "supervision": {
                "test_command": cfg["supervision"].get("test_command", ""),
            },
        })
    cfg = project.config
    if cfg["executor"].get("backend") == "hermes":
        prepare_hermes_profile(project, cfg, upgrade_profile=bool(getattr(args, "upgrade_profile", False)))
    if not os.path.exists(project.p("log.md")):
        with open(project.p("log.md"), "w", encoding="utf-8") as f:
            f.write("# Baton 修改日志\n\n执行者每完成一处修改追加一条；major 条目指向 reports/ 下的汇报。\n")
    if not os.path.exists(project.p("supervision.md")):
        with open(project.p("supervision.md"), "w", encoding="utf-8") as f:
            f.write("# Baton 监管记录\n\nOpus 每 30 分钟一次的检查结论。\n")
    ensure_shadow(project)
    project.record_id()
    print(f"已初始化 {project.dir}")
    print(f"项目外状态目录：{project.state_dir}")
    if not getattr(args, "no_statusline", False):
        print(statusline_install(project))
    print_snapshot_scope(project)


def _human_kb(kb):
    for unit in ("KB", "MB", "GB"):
        if kb < 1024 or unit == "GB":
            return f"{kb:.0f} {unit}" if unit == "KB" else f"{kb:.1f} {unit}"
        kb /= 1024.0


def _du(path):
    try:
        p = subprocess.run(["du", "-sk", path], capture_output=True, text=True, timeout=30, encoding="utf-8")
        if p.returncode == 0 and p.stdout.split():
            return _human_kb(int(p.stdout.split()[0]))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return "?"


def print_snapshot_scope(project, max_depth=4):
    """Show what snapshots cover: top-level entries, nested bulky dirs and nested projects."""
    store = SnapStore(project, "shadow")
    tracked = store.user_tracked()
    ignored_cache = {}
    sampled_dirs = set()

    def label(rel):
        bare = rel.rstrip("/")
        tracked_here = {p for p in tracked if p == bare or p.startswith(bare + "/")}
        is_excluded = excluded(rel)
        if tracked_here and is_excluded:
            result = "快照（用户仓库已跟踪）"
        else:
            result = "排除" if is_excluded else "快照"
        if bare in sampled_dirs:
            result += "（按抽样判定）"
        return result

    def excluded(rel):
        bare = rel.rstrip("/")
        full = os.path.join(project.root, bare)
        paths = [bare]
        if os.path.isdir(full) and not os.path.islink(full):
            files = []
            for dirpath, dirnames, filenames in os.walk(full):
                rel_dir = os.path.relpath(dirpath, project.root)
                dirnames[:] = [d for d in dirnames
                               if not os.path.islink(os.path.join(dirpath, d))
                               and not _shadow_prune_dir(d, rel_dir)]
                for filename in filenames:
                    files.append(os.path.relpath(os.path.join(dirpath, filename), project.root)
                                 .replace(os.sep, "/"))
                    if len(files) > 5000:
                        break
                if len(files) > 5000:
                    break
            if len(files) > 5000:
                sampled_dirs.add(bare)
                files = files[:5000]
            paths.extend(files)
        pending = [path for path in paths if path not in ignored_cache]
        if pending:
            ignored = shadow_excluded_many(store, pending, tracked=set())
            ignored_cache.update({path: path in ignored for path in pending})
        if ignored_cache.get(bare, False):
            return True
        if not os.path.isdir(full) or os.path.islink(full):
            return False
        return bool(paths[1:]) and all(ignored_cache.get(path, False) for path in paths[1:])

    print("快照覆盖范围（项目顶层条目 · 大小 · 是否排除）：")
    for name in sorted(os.listdir(project.root)):
        if name == ".git":
            continue
        full = os.path.join(project.root, name)
        is_dir = os.path.isdir(full) and not os.path.islink(full)
        print(f"  {label(name)}  {_du(full):>9}  {name + ('/' if is_dir else '')}")
    bulky, nested = [], []
    skip = {".git", ".baton", ".council", "node_modules", "__pycache__", "venv"}
    for dirpath, dirnames, _ in os.walk(project.root):
        rel_dir = os.path.relpath(dirpath, project.root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        keep = []
        for d in sorted(dirnames):
            rel = d if rel_dir == "." else os.path.join(rel_dir, d).replace(os.sep, "/")
            if d in skip or d.startswith(".venv") or os.path.islink(os.path.join(dirpath, d)):
                continue
            if is_project_dir(os.path.join(dirpath, d)):
                nested.append(rel)
                continue
            if depth >= 1 and d in BULKY_DIR_NAMES:
                bulky.append(rel)
            # Keep walking excluded parents so the confirmation list still names a
            # bulky directory such as exp/run1/checkpoints/ inside them.
            if depth + 1 < max_depth:
                keep.append(d)
        dirnames[:] = keep
    if bulky:
        print("嵌套的 data/models/outputs/logs/checkpoints 目录（大小 · 是否排除）：")
        for rel in bulky:
            print(f"  {label(rel + '/')}  {_du(os.path.join(project.root, rel)):>9}  {rel}/")
    if nested:
        print("嵌套项目（有自己的 Baton，外层任务的回滚不会改动它们）：" + "，".join(nested))
    print(f"排除规则在 {os.path.join(project.snap_git_dir, 'info', 'exclude')}，项目内的 .gitignore 也生效，"
          "项目目录以上的 .gitignore 不生效；用户仓库已跟踪的文件即使被忽略也会进快照。"
          "请和用户确认范围；要调整就编辑这个文件，下次快照起生效。")


def cmd_statusline(project, args):
    if args.action == "install":
        print(statusline_install(project, force=args.force))
    elif args.action == "uninstall":
        print(statusline_uninstall(project))
    else:
        print(statusline_report(project))


def cmd_new(project, args):
    project.require_init()
    check_task(args.task)
    os.makedirs(project.task_dir(args.task), exist_ok=True)
    brief = os.path.join(project.task_dir(args.task), "brief.md")
    if os.path.exists(brief):
        die(f"简报已存在：{brief}")
    if args.brief:
        shutil.copyfile(args.brief, brief)
    else:
        text = read_text(os.path.join(TEMPLATES, "brief.md")).replace("{{TASK}}", args.task)
        with open(brief, "w", encoding="utf-8") as f:
            f.write(text)
    print(brief)


def running_task(project, exclude=None):
    """Another task of this project with a running leg or a live codex, if any."""
    for task in project.tasks():
        if task == exclude:
            continue
        st = project.read_state(task, {}) or {}
        if not st.get("legs"):
            continue
        st = refresh_runner_liveness(project, task, st)
        leg = current_leg(st)
        if leg and (leg.get("status") == "running" or
                    (leg.get("monitor") or {}).get("status") == "running" or codex_alive(leg)):
            return task, leg["n"]
    return None


def launch_leg(project, task, kind, prompt, force_escalate=False):
    os.makedirs(project.dir, exist_ok=True)
    os.makedirs(project.state_dir, exist_ok=True)
    with (platform_support.exclusive_lock(project.launch_lock_path()) if os.name == "nt"
          else open(project.launch_lock_path(), "w", encoding="utf-8")) as launch_lock:
        if fcntl:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)  # one launch at a time per project
        st = project.read_state(task, {}) or {}
        if st.get("status") == "closed":
            die(f"任务 {task} 已 closed，不能 start 或 resume")
        st = refresh_runner_liveness(project, task, st) if st.get("legs") else st
        cfg = config_for_state(project, st)
        require_supported_executor(cfg)
        escalation = escalation_decision(project, task, st, force=force_escalate)
        effective_cfg = _effective_executor_config(cfg, escalation)
        if cfg["executor"].get("backend") == "hermes":
            hermes_preflight(project, cfg)
        if st.get("config_snapshot") and st.get("config_input_hash") != config_input_hash(project):
            print("提示：本任务不生效，需 baton amend --config")
            try:
                changed_keys = config_different_keys(project.config, cfg)
            except ConfigError as e:
                print(f"当前配置无效，继续使用任务快照：{e}")
            else:
                if changed_keys:
                    print("与快照不同的配置键：" + ", ".join(changed_keys[:10])
                          + ("（最多显示 10 个）" if len(changed_keys) > 10 else ""))
        leg = current_leg(st) if st else None
        if leg and (leg.get("monitor") or {}).get("status") == "running":
            die(f"任务 {task} 的监督简报仍在生成；先 baton wait {task}")
        if leg and leg.get("status") == "running":
            die(f"任务 {task} 第 {leg['n']} 轮仍在运行；先 baton wait 或 baton stop")
        if codex_alive(leg):
            die(f"上一轮的 codex 进程（pid {leg['codex_pid']}）还在运行；先 baton stop {task}")
        other = running_task(project, exclude=task)
        if other:
            die(f"同一项目里任务 {other[0]} 的第 {other[1]} 轮还在运行，两个执行者不能同时改一个工作区")
        if st:
            surface_unreported(project, task, st, "续跑")
        n = len(st.get("legs", [])) + 1
        os.makedirs(project.runs_dir(task), exist_ok=True)
        ready_path = memory_mcp_ready_path(project, task, n)
        try:
            os.remove(ready_path)
        except OSError:
            pass
        if cfg["executor"].get("backend") == "hermes" and memory_installed(cfg):
            ok, reason = memory_mcp_preflight(project, task, n, cfg)
            if not ok:
                die("baton-memory preflight 失败：" + reason)
        prompt_path = project.leg_path(task, n, "prompt.md")
        if os.path.exists(prompt_path):
            die(f"{prompt_path} 已经存在，和任务状态里的轮次对不上（项目目录可能换过位置，或状态目录变了）；"
                "为免覆盖旧的运行记录，先查明原因；移动项目用 baton init --relink")
        original_prompt = prompt
        prompt, memory_meta = memory_prompt_for_leg(project, task, prompt, st=st, cfg=cfg)
        if memory_meta.get("context"):
            if cfg["executor"].get("backend") == "codex" and memory_meta.get("core"):
                prompt = (memory_meta["core"] + "\n\n" + original_prompt.rstrip() +
                          "\n\n" + memory_meta["context"])
            elif cfg["executor"].get("backend") == "hermes":
                prompt = original_prompt.rstrip() + "\n\n" + memory_meta["context"]
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)

        print(codex_quota.describe(safe_quota(block=True, notify=True)))

        start_ref, err = try_checkpoint(project, task, f"leg-{n:03d}-start", st=st)
        if err:
            print(f"注意：本轮起点回滚点没建成：{err}")
        last_path = project.leg_path(task, n, "last.json")
        if cfg["executor"].get("backend") == "hermes":
            cmd = hermes_command(project, kind, st.get("thread_id"), prompt, cfg=effective_cfg)
            if not effective_cfg["executor"].get("sandbox_exec", True):
                print("WARN：executor.sandbox_exec=false，Hermes 将不受 Seatbelt 写入边界保护。")
        else:
            cmd = codex_command(project, kind, st.get("thread_id"), last_path, cfg=effective_cfg)
        t0 = now()
        log_size = file_size(project.p("log.md"))

        def add_leg(s):
            s.setdefault("task", task)
            s.setdefault("created_at", t0)
            s.setdefault("legs", [])
            s["legs"].append({
                "n": n, "kind": kind, "status": "running", "started_at": t0,
                "backend": cfg["executor"].get("backend", "codex"),
                "hermes_profile": cfg["executor"].get("hermes_profile", "baton-exec-v2"),
                "model": effective_cfg["executor"].get("model"),
                "reasoning_effort": effective_cfg["executor"].get("reasoning_effort"),
                "base_model": cfg["executor"].get("model"),
                "escalated": bool(escalation.get("escalated")),
                "escalation_reason": escalation.get("reason", ""),
                "escalation_auto": bool(escalation.get("auto")),
                "escalation_auto_count": int(escalation.get("auto_count", 0) or 0),
                "start_ref": start_ref, "cmd": cmd, "prompt": prompt_path,
                "events": project.leg_path(task, n, "events.jsonl"), "last": last_path,
                "stderr": project.leg_path(task, n, "stderr.log"), "log_size_at_start": log_size,
                "capacity_retries": [], "capacity_wait_seconds": 0.0,
                "alerts": [], "alert_keys": [], "probe_hashes": {},
                "last_file_change_at": t0, "last_rule_terminal_at": t0,
                "memory_pushed": list(memory_meta.get("pushed", [])),
                "memory_core": memory_meta.get("core", ""),
                "memory_core_truncated": bool(memory_meta.get("core_truncated", False)),
                "memory_withdrawals": list(memory_meta.get("withdrawals", [])),
                "memory_error": memory_meta.get("error", ""),
                "memory_fallback_reason": memory_meta.get("fallback_reason", ""),
                "check_interval_minutes": cfg["supervision"].get("first_check_minutes", 10),
                "next_check_at": t0 + float(cfg["supervision"].get("first_check_minutes", 10)) * 60,
            })
            s["status"] = "running"
            # resuming the executor is itself a supervision point: Opus has just looked at everything
            s["last_supervision_at"] = t0
            s["supervision"] = {"leg": n, "events_offset": 0, "log_offset": log_size, "ref": start_ref, "since": t0}
            s.pop("pending_supervision", None)
            s.pop("pending_drift", None)
            s.pop("supervision_action", None)
            s["supervision_fallback"] = False
            if escalation.get("auto"):
                s["escalation"] = dict(s.get("escalation") or {},
                                        auto_count=escalation.get("auto_count", 0),
                                        last_reason=escalation.get("reason", ""),
                                        last_leg=n)
        project.update_state(task, add_leg)

        log = open(project.leg_path(task, n, "runner.log"), "w", encoding="utf-8")
        try:
            runner = platform_support.detached_popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                       "_run", task, str(n)],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=project.root)
        finally:
            log.close()
        if hasattr(runner, "poll"):
            _DETACHED_RUNNERS.append(runner)

        def set_pid(s):
            current_leg(s)["runner_pid"] = runner.pid
        project.update_state(task, set_pid)
    ex = cfg["executor"]
    print(f"BATON::STARTED task={task} leg={n} kind={kind} model={ex['model']} "
          f"effort={ex['reasoning_effort']} service_tier={ex.get('service_tier', 'default')} "
          f"actual_model={effective_cfg['executor']['model']}"
          + (" 升级" if escalation.get("escalated") else "")
          + (f" escalation_reason={escalation.get('reason')}" if escalation.get("escalated") else "") + " "
          f"judge={cfg['conductor']['model']} runner_pid={runner.pid} monitor={cfg['monitor']['model']}")
    baseline = st.get("baseline_verify")
    if st.get("baseline_pending"):
        print("基线验证在 runner 中进行")
    elif baseline:
        print("验证基线：" + _verification_summary(baseline))
    print(f"回滚点：{start_ref or '无'}")
    interval = cfg["supervision"]["interval_minutes"]
    if interval:
        next_step = f"下一步：用后台方式运行 `baton wait {task}`，leg 结束或 {interval} 分钟监管时间到会唤醒你。"
    else:
        next_step = "下一步：用后台方式运行 `baton wait {task}`；本模式只在 leg 结束或额度不足时返回。".format(task=task)
    print(next_step)


def cmd_start(project, args):
    require_supported_executor(project.config)
    project.require_init()
    check_task(args.task)
    brief_path = os.path.join(project.task_dir(args.task), "brief.md")
    brief = read_text(brief_path)
    if not brief.strip():
        die(f"缺少任务简报 {brief_path}；先 baton new {args.task} 并填写")
    if "（待填）" in brief or "{{TASK}}" in brief:
        die(f"简报里还有未填写的模板占位（「（待填）」或 {{{{TASK}}}}）：{brief_path}")
    st = project.read_state(args.task, {}) or {}
    if st.get("status") == "closed":
        die(f"任务 {args.task} 已 closed，不能 start")
    cfg = config_for_state(project, st)
    scope = parse_scope(brief, required=not bool(st.get("legs")))
    if scope and scope.get("verify") == "auto" and not (cfg.get("supervision") or {}).get("test_command"):
        die("supervision.test_command 为空；范围块 verify=auto 时必须配置验证命令，或写 verify=none")
    if st.get("thread_id"):
        die(f"任务 {args.task} 已有 Codex 会话 {st['thread_id']}；继续请用 baton resume")
    r = subprocess.run([platform_support.executable("git"), "check-ref-format", f"refs/baton/{args.task}/base"])
    if r.returncode != 0:
        die(f"任务名 {args.task} 不能用作 git ref，请换一个")
    write_baton_gitignore(project)
    if is_git(project.root) and baton_statusline_installed(project):
        warn = ensure_settings_ignored(project)
        if warn:
            print(warn)
    if not st.get("base_ref"):
        if not st.get("legs"):
            # a task started by v2 keeps every checkpoint in the shadow repo
            st = project.update_state(args.task, lambda s: s.update(snapshot_store="shadow", task=args.task,
                                                                       scope=scope or {}, config_snapshot=cfg,
                                                                       config_input_hash=config_input_hash(project)))
            write_freeze_baseline(project, args.task, scope or {}, cfg)
            manifest = read_json(os.path.join(freeze_dir(project, args.task), "manifest.json"), {}) or {}
            project.update_state(args.task, lambda s: s.update(freeze_manifest=manifest,
                                                               frozen_dir=freeze_dir(project, args.task),
                                                               baseline_pending=True))
            st = project.read_state(args.task, {}) or st
        ref, err = try_checkpoint(project, args.task, "base", st=st)
        if err:
            die(f"任务起点回滚点没建成：{err}")
        project.update_state(args.task, lambda s: s.update(base_ref=ref, task=args.task))
    prompt = (protocol_text(project, args.task)
              + f"\n\n---\n\n# 任务简报（{args.task}）\n\n" + brief)
    launch_leg(project, args.task, "start", prompt)


def message_from(args):
    """The conductor's message (file or --message), validated before anything else happens."""
    if getattr(args, "message_file", None):
        if not os.path.isfile(args.message_file):
            die(f"找不到消息文件 {args.message_file}")
        message = read_text(args.message_file)
        if not message.strip():
            die(f"消息文件 {args.message_file} 是空的")
        return message
    if not (getattr(args, "message", None) or "").strip():
        die("需要指挥消息：给出消息文件，或用 --message TEXT")
    return args.message


def archive_conductor_message(project, task, message, kind):
    directory = os.path.join(project.task_dir(task), "messages")
    os.makedirs(directory, exist_ok=True)
    existing = [name for name in os.listdir(directory) if re.match(r"^M\d{3}-", name)]
    number = max([int(name[1:4]) for name in existing] or [0]) + 1
    safe_kind = re.sub(r"[^A-Za-z0-9_-]+", "-", str(kind or "message")).strip("-") or "message"
    path = os.path.join(directory, f"M{number:03d}-{safe_kind}.md")
    message_hash = hashlib.sha256(message.encode()).hexdigest()
    record = append_ledger(project, {"type": "message", "task": task, "number": f"M{number:03d}",
                                     "kind": kind or "message", "message": message,
                                     "full": message, "sha256": message_hash, "message_sha256": message_hash})
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(message)
    return path


def _resume_ruling_gate(project, task, st, action="resume"):
    """Require a current brief and verdict before continuing a supervised leg."""
    leg = current_leg(st) or {}
    monitor = leg.get("monitor") or {}
    curation_monitor = st.get("curation_monitor") or {}
    dossier_id = (leg.get("dossier") or {}).get("id") or monitor.get("id")
    if leg.get("executor_status", leg.get("status")) not in ("report", "done", "violation") or not dossier_id:
        return None
    if monitor.get("status") in ("running", "failed") or curation_monitor.get("status") in ("running", "failed"):
        active_monitor = monitor if monitor.get("status") in ("running", "failed") else curation_monitor
        state = "仍在运行" if active_monitor["status"] == "running" else "已失败"
        die(f"卷宗 {dossier_id} 的 monitor {state}；可以等待、baton monitor {task} {dossier_id} --rerun 或问用户；"
            f"监督简报完成后先 baton rule，再 {action}")
    if not verify_ledger(project):
        die("账本校验失败，拒绝续跑")
    good, error = verify_dossier(project, dossier_id)
    if not good:
        die("卷宗校验失败，拒绝续跑：" + error)
    brief, dossier_hash = _dossier_brief(project, task, dossier_id)
    verdict = _latest_ledger_record(ledger_records(project), "verdict", task, dossier=dossier_id)
    if not brief or not verdict or verdict.get("dossier_hash") != dossier_hash \
            or verdict.get("brief_seq") != brief.get("seq") \
            or verdict.get("brief_record_hash") != brief.get("record_hash"):
        die(f"卷宗 {dossier_id} 缺少绑定最新简报的裁决，请先 baton rule {task} {dossier_id} <裁决文件>；再 {action}")
    if dossier_manifest(project, dossier_id).get("type") == "violation":
        conclusion = verdict.get("conclusion")
        if conclusion == "ASK_USER":
            die("ASK_USER：等用户决定后用 --rerule 改裁决，当前不能 resume")
        checked = check_freeze(project, task, st)
        if checked.get("violations"):
            if conclusion == "REVERT":
                die(f"冻结仍有锁定级违规，请先 baton restore-frozen {task}")
            if conclusion == "ADOPT":
                die(f"amend --adopt 尚未完成，用户须在自己的终端运行 baton amend {task} --adopt <路径> --user-approved")
    return verdict


def _resume_message(project, args, st):
    verdict_id = getattr(args, "verdict", None)
    decision_id = getattr(args, "decision", None)
    if (verdict_id or decision_id) and (getattr(args, "message_file", None) or getattr(args, "message", None)):
        die("--verdict/--decision 不能与消息文件或 --message 同时使用")
    if verdict_id and decision_id:
        die("--verdict 和 --decision 不能同时使用")
    if verdict_id or decision_id:
        if not verify_ledger(project):
            die("账本校验失败，拒绝读取裁决/决定消息")
        records = ledger_records(project)
        if verdict_id:
            record = _latest_ledger_record(records, "verdict", args.task, dossier=verdict_id)
            if not record:
                die(f"卷宗 {verdict_id} 尚未裁决，请先 baton rule")
            if _dossier_leg(project, args.task, verdict_id, st) is None:
                die(f"卷宗 {verdict_id} 不属于本任务轮次")
            return record["rendered"]
        if not current_leg(st) or current_leg(st).get("status") != "decision":
            die("只有以 decision 结束的轮次才能用 --decision 续跑")
        record = _latest_ledger_record(records, "decision", args.task, decision_id=decision_id)
        if not record:
            die(f"决定 {decision_id} 尚未入账，请先 baton decide")
        return record["decision"]
    return message_from(args)


def cmd_resume(project, args, message=None):
    project.require_init()
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    if st.get("status") == "closed":
        die(f"任务 {args.task} 已 closed，不能 resume")
    verdict = _resume_ruling_gate(project, args.task, st)
    current = current_leg(st) or {}
    if current.get("escalated"):
        conclusion = str((verdict or {}).get("conclusion", "")).upper()
        if current.get("status") in {"blocked", "failed"} or conclusion == "REVISE":
            project.update_state(args.task, lambda s: s.update(escalation=dict(
                s.get("escalation") or {}, auto_suppressed=True,
                suppressed_reason="升级轮未通过，等待 conductor 决定")))
            st = project.load_state(args.task)
        elif conclusion in {"APPROVE", "ACCEPT"}:
            project.update_state(args.task, lambda s: s.update(escalation=dict(
                s.get("escalation") or {}, auto_suppressed=False,
                suppressed_reason="")))
            st = project.load_state(args.task)
    dossier_id = (current.get("dossier") or {}).get("id") or (current.get("monitor") or {}).get("id")
    key = (_revision_key(project, dossier_id) or dossier_id) if dossier_id and os.path.isdir(dossier_path(project, dossier_id)) else None
    count = _revision_count(project, args.task, dossier_id) if key else 0
    if count >= 3 and not getattr(args, "user_approved_continue", False):
        die(f"REVISE 已累计 {count} 次（事项 {key}），请用户确认后加 --user-approved-continue，并逐字确认任务名")
    if count >= 3:
        _require_task_confirmation(args.task)
    message = message if message is not None else _resume_message(project, args, st)
    if verdict and not getattr(args, "verdict", None):
        print("提示：这一轮没有引用裁决原文，使用普通指挥消息续跑。")
    if not getattr(args, "_message_archived", False):
        archive_conductor_message(project, args.task, message, getattr(args, "kind", "回复"))
    if not st.get("thread_id"):
        leg = current_leg(st)
        tid = thread_id_from(leg["events"]) if leg else None
        if not tid:
            die("找不到 Codex 会话 id，无法续跑；可能首轮没有成功启动，改用 baton start")
        project.update_state(args.task, lambda s: s.update(thread_id=tid))
    cfg = config_for_state(project, st)
    amendment_hint = amendments_text(st)
    prompt = (f"[Baton 指挥 · {args.kind}]\n\n{message.strip()}\n\n"
              "（继续遵守 Baton 执行协议：小修改记 log，大修改写汇报后停下，"
              "需要决策时写决策请求后停下；本轮结束按 JSON schema 输出。"
              f"当前 major 阈值：{cfg['change_size']['major_files']} 个文件或 {cfg['change_size']['major_lines']} 行；"
              f"验证命令：{cfg['supervision'].get('test_command') or '按简报'}。）\n"
              + amendment_hint)
    launch_leg(project, args.task, "resume", prompt, force_escalate=bool(getattr(args, "escalate", False)))


def _interrupt(proc):
    if os.name == "nt":
        platform_support.stop_tree(proc.pid, (20, 10, 5))
        return
    for sig, grace in ((signal.SIGINT, 20), (signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


class _Terminated(Exception):
    pass


def _raise_terminated(signum, frame):
    # one shutdown is enough: ignore further TERM/HUP while we stop codex and record the leg
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    raise _Terminated(f"signal {signum}")


def cmd_run(project, args):
    """Internal: the detached leg runner. Always records a result, even when it is killed."""
    task, n = args.task, int(args.n)
    signal.signal(signal.SIGTERM, _raise_terminated)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _raise_terminated)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, _raise_terminated)
    st = project.load_state(task)
    leg = st["legs"][n - 1]
    stop_flag = project.leg_path(task, n, "stop")
    proc, reason, error = None, None, None
    violation = None
    rc = -1
    result = None
    capacity_final = False
    capacity_retry_count = 0
    finished_ok = False
    memory_tool_reason = None
    try:
        cfg = config_for_state(project, st)
        require_supported_executor(cfg)
        # Keep retries on the model selected when this leg was launched.  The
        # task snapshot itself remains on the default model for later legs.
        if leg.get("model"):
            cfg = json.loads(json.dumps(cfg))
            cfg["executor"]["model"] = leg["model"]
            if leg.get("reasoning_effort"):
                cfg["executor"]["reasoning_effort"] = leg["reasoning_effort"]
        if n == 1 and st.get("baseline_pending"):
            # The baseline must observe the untouched worktree, before Codex is
            # started.  A failed or timed-out baseline is still recorded and does
            # not prevent the executor from running.
            baseline_verify = run_verification(project, task, st, cfg, baseline=True)
            st = project.update_state(task, lambda s: s.update(
                baseline_verify=baseline_verify, baseline_pending=False))
        limit = float(cfg["supervision"].get("max_unsupervised_minutes") or 0)
        retry_cfg = capacity_retry_config(cfg)
        try:
            check_seconds = float((cfg.get("freeze") or {}).get("check_seconds", 60) or 60)
        except (TypeError, ValueError):
            check_seconds = 60.0
        last_freeze_check = now()
        last_rule_check = 0.0
        try:
            rule_check_seconds = float((cfg.get("supervision") or {}).get("rule_check_seconds", 60) or 60)
        except (TypeError, ValueError):
            rule_check_seconds = 60.0
        immediate_used = 0
        delayed_used = 0
        hermes_schema_retry = False
        pending_attempt_cmd = None
        pending_attempt_prompt = None

        def set_codex_pid(pid, exited=False):
            def update(s):
                lg = current_leg(s)
                if lg and lg.get("n") == n:
                    lg["codex_pid"] = pid
                    lg["codex_exited"] = exited
            project.update_state(task, update)

        def add_wait(phase, retry_number, delay):
            started = now()
            wait = {"retry": retry_number, "phase": phase,
                    "started_at": started if delay else None,
                    "next_retry_at": started + delay}
            def update(s):
                lg = current_leg(s)
                if lg and lg.get("n") == n:
                    lg["capacity_wait"] = wait
                    lg["codex_pid"] = None
                    lg["codex_exited"] = True
            project.update_state(task, update)

        def finish_wait():
            def update(s):
                lg = current_leg(s)
                if not lg or lg.get("n") != n:
                    return
                wait = lg.pop("capacity_wait", None)
                if isinstance(wait, dict) and wait.get("started_at") is not None:
                    ended = now()
                    elapsed = max(0.0, ended - wait["started_at"])
                    lg["capacity_wait_seconds"] = float(lg.get("capacity_wait_seconds", 0) or 0) + elapsed
                    lg.setdefault("capacity_wait_intervals", []).append(
                        {"started_at": wait["started_at"], "ended_at": ended})
            project.update_state(task, update)

        def wait_for_retry():
            """Wait without blocking stop handling or counting the wait as unsupervised."""
            nonlocal reason
            while True:
                current = project.read_state(task, {}) or st
                current_leg_state = current_leg(current) or leg
                wait = current_leg_state.get("capacity_wait") or {}
                remaining = float(wait.get("next_retry_at", now())) - now()
                if os.path.exists(stop_flag):
                    reason = "stopped"
                    finish_wait()
                    return False
                if remaining <= 0:
                    finish_wait()
                    return True
                time.sleep(min(0.2, max(0.01, remaining)))

        def pre_retry_check():
            nonlocal violation, reason
            if os.path.exists(stop_flag):
                reason = "stopped"
                return False
            checked = check_freeze(project, task, project.read_state(task, {}) or st)
            if checked.get("violations"):
                violation = checked
                reason = "violation"
                return False
            return True

        def run_attempt(attempt_cmd, attempt_prompt):
            """Run one executor invocation and return only data belonging to it."""
            nonlocal proc, reason, violation, st, last_freeze_check, last_rule_check
            is_hermes = cfg["executor"].get("backend") == "hermes"
            if is_hermes:
                hermes_preflight(project, cfg)
            try:
                os.remove(leg["last"])
            except FileNotFoundError:
                pass
            event_offset = file_size(leg["events"])
            stderr_offset = file_size(leg["stderr"])
            remote_refs_before = git_remote_refs(project.root)
            with open(leg["events"], "ab") as fout, open(leg["stderr"], "ab") as ferr:
                if is_hermes:
                    proc = platform_support.detached_popen(attempt_cmd, cwd=project.root, stdin=subprocess.DEVNULL,
                                            stdout=fout, stderr=ferr,
                                            env=hermes_executor_env(project, task, n, leg["started_at"], cfg, st))
                else:
                    with open(attempt_prompt, "rb") as fin:
                        proc = platform_support.detached_popen(attempt_cmd, cwd=project.root, stdin=fin, stdout=fout,
                                                stderr=ferr)
            set_codex_pid(proc.pid, exited=False)
            while proc.poll() is None:
                time.sleep(0.2)
                if proc.poll() is not None:
                    break  # finished on its own during the sleep: its result wins over a late stop
                if not st.get("thread_id"):
                    tid = thread_id_from(leg["events"])
                    if tid:
                        st = project.update_state(task, lambda s: s.update(thread_id=tid))
                current_state = project.read_state(task, {}) or st
                if current_state.get("supervision_action") == "breaking":
                    reason = "breaking"
                    _persist_leg_stop_reason(project, task, n, "breaking")
                    _interrupt(proc)
                    break
                if os.path.exists(stop_flag):
                    stop_reason = read_text(stop_flag).strip()
                    reason = "drift_unhandled" if stop_reason == "drift_unhandled" else "stopped"
                    if reason == "drift_unhandled":
                        _persist_leg_stop_reason(project, task, n, reason)
                    _interrupt(proc)
                    break
                pending_drift = current_state.get("pending_drift") or {}
                if pending_drift.get("deadline") and now() >= float(pending_drift["deadline"]):
                    reason = "drift_unhandled"
                    _persist_leg_stop_reason(project, task, n, reason)
                    with open(stop_flag, "w", encoding="utf-8") as stream:
                        stream.write(reason)
                    _interrupt(proc)
                    break
                if now() - last_freeze_check >= max(0.1, check_seconds):
                    last_freeze_check = now()
                    checked = check_freeze(project, task, current_state)
                    if checked.get("violations"):
                        violation = checked
                        reason = "violation"
                        _interrupt(proc)
                        break
                    if monitor_enabled(cfg) and now() - last_rule_check >= max(0.1, rule_check_seconds):
                        last_rule_check = now()
                        new_alerts = run_rule_checks(project, task, current_state, current_leg(current_state) or leg)
                        current_state = project.read_state(task, {}) or st
                        current_leg_state = current_leg(current_state) or leg
                        if new_alerts:
                            maybe_start_monitor_check(project, task, current_state, current_leg_state, force=True)
                        else:
                            store = project.store(task, current_state)
                            if store and current_leg_state.get("start_ref") and current_leg_state.get("probe_ref"):
                                files_changed = shortstat(store, current_leg_state["start_ref"], current_leg_state["probe_ref"])[0]
                                if files_changed >= 3 and not current_leg_state.get("last_check"):
                                    maybe_start_monitor_check(project, task, current_state, current_leg_state, force=True)
                    current_state = project.read_state(task, {}) or st
                if monitor_enabled(cfg):
                    maybe_start_monitor_check(project, task, current_state, current_leg(current_state) or leg)
                last_sup = current_state.get("last_supervision_at", leg["started_at"])
                elapsed_sup = now() - last_sup - capacity_wait_seconds(current_leg(current_state), since=last_sup)
                fallback = bool(current_state.get("supervision_fallback") or (current_leg(current_state) or {}).get("supervision_fallback"))
                if limit and (not monitor_enabled(cfg) or fallback) and elapsed_sup > limit * 60:
                    reason = "paused_unsupervised"
                    _persist_leg_stop_reason(project, task, n, reason)
                    _interrupt(proc)
                    break
            rc_value = proc.wait()
            set_codex_pid(None, exited=True)
            remote_refs_after = git_remote_refs(project.root)
            call_leg = dict(leg, backend=cfg["executor"].get("backend", "codex"))
            call_leg["thread_id"] = (project.read_state(task, {}) or {}).get("thread_id")
            record_external_actions(project, task, n, remote_refs_before, remote_refs_after,
                                    leg_terminal_calls(project, task, call_leg, since=leg.get("started_at")))
            current_state = project.read_state(task, {}) or st
            active_wait = (current_leg(current_state) or {}).get("capacity_wait") or {}
            if active_wait.get("phase") == "immediate":
                finish_wait()
            result_error = ""
            hermes_stdout = read_bytes_since(leg["events"], event_offset) or ""
            hermes_stderr = read_bytes_since(leg["stderr"], stderr_offset) or ""
            if is_hermes:
                session_id = hermes_session_id(hermes_stderr)
                if session_id:
                    st = project.update_state(task, lambda s: s.update(thread_id=session_id))
                parsed, result_error = parse_hermes_result(hermes_stdout, hermes_stderr, rc_value)
                if parsed is not None:
                    write_json(leg["last"], parsed)
                current_result = parsed
            else:
                current_result = normalize_executor_result(read_json(leg["last"]))
            if current_result is not None and not isinstance(current_result, dict):
                current_result = {"status": "unknown", "summary": json.dumps(current_result, ensure_ascii=False)[:4000],
                                  "artifact": "", "change_size": "unknown"}
            if current_result is None and os.path.exists(leg["last"]):
                current_result = {"status": "unknown", "summary": read_text(leg["last"])[:4000],
                                  "artifact": "", "change_size": "unknown"}
            new_events, _ = read_events(leg["events"], event_offset)
            failed_call = rc_value != 0 or not valid_executor_result(current_result)
            capacity = (not reason and failed_call and
                        is_capacity_failure(leg["events"], leg["stderr"], event_offset,
                                            stderr_offset, rc_value, current_result))
            has_output = bool(hermes_stdout.strip()) if is_hermes else any(ev.get("type") == "item.completed"
                             and isinstance(ev.get("item"), dict)
                             and ev["item"].get("type") != "error" for ev in new_events)
            return rc_value, current_result, capacity, has_output, result_error

        attempt = 0
        while True:
            current_state = project.read_state(task, {}) or st
            thread_id = current_state.get("thread_id") or st.get("thread_id")
            if pending_attempt_cmd is not None:
                attempt_cmd = pending_attempt_cmd
                attempt_prompt = pending_attempt_prompt
                pending_attempt_cmd = pending_attempt_prompt = None
            elif attempt and thread_id:
                retry_prompt = project.leg_path(task, n, f"retry-{attempt:03d}.prompt.md")
                with open(retry_prompt, "w", encoding="utf-8") as stream:
                    stream.write("上一次调用因模型容量不足中断，请从中断处继续当前任务，遵守 Baton 执行协议，本轮结束按 JSON schema 输出。")
                if cfg["executor"].get("backend") == "hermes":
                    retry_text = read_text(retry_prompt)
                    attempt_cmd = hermes_command(project, "resume", thread_id, retry_text, cfg=cfg)
                else:
                    attempt_cmd = codex_command(project, "resume", thread_id, leg["last"], cfg=cfg)
                attempt_prompt = retry_prompt
            else:
                attempt_cmd = list(leg["cmd"])
                attempt_prompt = leg["prompt"]
            rc, result, capacity, has_output, result_error = run_attempt(attempt_cmd, attempt_prompt)
            attempt += 1
            if st.get("thread_id") is None:
                tid = thread_id_from(leg["events"])
                if tid:
                    st = project.update_state(task, lambda s: s.update(thread_id=tid))
            finished_ok = rc == 0 and valid_executor_result(result)
            if (cfg["executor"].get("backend") == "hermes" and rc == 0 and not finished_ok
                    and not hermes_schema_retry and not reason and not error):
                current = project.read_state(task, {}) or st
                resume_id = current.get("thread_id") or st.get("thread_id")
                if resume_id:
                    hermes_schema_retry = True
                    retry_prompt = project.leg_path(task, n, "result-retry.prompt.md")
                    with open(retry_prompt, "w", encoding="utf-8") as stream:
                        stream.write("只重新输出本轮结果，不要继续修改文件。必须严格输出 BATON_RESULT_BEGIN、JSON、BATON_RESULT_END。校验错误：" + (result_error or "结果缺失"))
                    pending_attempt_cmd = hermes_command(project, "resume", resume_id, read_text(retry_prompt), cfg=cfg)
                    pending_attempt_prompt = retry_prompt
                    continue
            if not capacity or finished_ok or reason or error:
                break
            if has_output:
                capacity_retry_count = 0
            phase, delay, immediate_used, delayed_used = next_capacity_retry(
                retry_cfg, immediate_used, delayed_used, has_output)
            if phase is None:
                capacity_final = True
                break
            if phase == "delayed":
                add_wait(phase, capacity_retry_count + 1, delay)
                if not wait_for_retry():
                    break
            else:
                add_wait(phase, capacity_retry_count + 1, 0)
            if not pre_retry_check():
                if phase == "immediate":
                    finish_wait()
                break
            capacity_retry_count += 1
            retry_at = now()
            retry_record = {"at": retry_at, "time": stamp(retry_at, fmt="%Y-%m-%d %H:%M:%S"),
                            "phase": phase, "retry": capacity_retry_count}
            def record_retry(s):
                lg = current_leg(s)
                if lg and lg.get("n") == n:
                    lg.setdefault("capacity_retries", []).append(retry_record)
            project.update_state(task, record_retry)
    except BaseException as e:  # noqa: BLE001 — killed or crashed: stop codex, still record the leg
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        if os.name == "nt":
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        error = f"runner 中断（{type(e).__name__}: {e}）"
        if proc is not None and proc.poll() is None:
            _interrupt(proc)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if os.name == "nt":
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    if proc is not None:
        rc = proc.wait()
    result = normalize_executor_result(read_json(leg["last"]))
    if result is not None and not isinstance(result, dict):
        result = {"status": "unknown", "summary": json.dumps(result, ensure_ascii=False)[:4000],
                  "artifact": "", "change_size": "unknown"}
    if result is None and os.path.exists(leg["last"]):
        result = {"status": "unknown", "summary": read_text(leg["last"])[:4000],
                  "artifact": "", "change_size": "unknown"}
    finished_ok = rc == 0 and valid_executor_result(result)
    if finished_ok:
        status = result["status"]  # the executor's own result beats a stop that arrived too late
    elif error:
        status = "failed"
    elif reason:
        status = reason
    else:
        status = "failed"
    action_state = project.read_state(task, {}) or st
    persisted_stop = ((current_leg(action_state) or {}).get("stop_reason")
                      or action_state.get("supervision_action"))
    pending_at_end = action_state.get("pending_drift") or {}
    if not persisted_stop and pending_at_end.get("deadline") and now() >= float(pending_at_end["deadline"]):
        persisted_stop = "drift_unhandled"
        _persist_leg_stop_reason(project, task, n, persisted_stop)
    if persisted_stop in {"breaking", "drift_unhandled", "paused_unsupervised", "stopped"}:
        status = persisted_stop
    if status in ("done", "decision", "report", "blocked"):
        if action_state.get("supervision_action") == "breaking":
            status = "breaking"
        elif (action_state.get("pending_drift") or {}).get("deadline") and now() >= float(
                (action_state.get("pending_drift") or {}).get("deadline")):
            status = "drift_unhandled"

    checked_end = check_freeze(project, task, project.read_state(task, {}) or st)
    if checked_end.get("violations"):
        violation = checked_end
        if status in ("done", "decision", "report", "blocked"):
            status = "violation"
    verify = run_verification(project, task, project.read_state(task, {}) or st,
                              config_for_state(project, project.read_state(task, {}) or st),
                              leg_n=n,
                              skip_reason="本轮以 VIOLATION 结束，跳过验证" if violation else None)
    if config_for_state(project, project.read_state(task, {}) or st)["executor"].get("backend") == "hermes" \
            and memory_installed(config_for_state(project, project.read_state(task, {}) or st)):
        memory_ok, memory_tool_reason = memory_mcp_status(project, task, n, leg.get("started_at", now()))
        if memory_ok:
            memory_tool_reason = None
    end_ref, cp_err = try_checkpoint(project, task, f"leg-{n:03d}-end")
    diff = None
    if end_ref and leg.get("start_ref"):
        try:
            diff = shortstat(project.store(task), leg["start_ref"], end_ref)
        except Exception:  # noqa: BLE001
            diff = None
    events, _ = read_events(leg["events"])
    dg = digest(events)
    log_size = file_size(project.p("log.md"))
    stderr_tail = read_text(leg["stderr"])[-1500:]
    notes = [x for x in (error if not finished_ok else None,
                         "收到停止指令时执行者已自己结束，按它的结果记录" if finished_ok and (reason or error) else None,
                         f"本轮终点回滚点没建成：{cp_err}" if cp_err else None) if x]
    monitor_cfg = config_for_state(project, project.read_state(task, {}) or st)
    dossier_id = None
    if monitor_enabled(monitor_cfg) and status in ("report", "done", "violation"):
        # The dossier is assembled from the immutable end checkpoint after the executor leg
        # has recorded its own result.  The monitor lifecycle below changes only external state.
        dossier_id = dossier_id_for(project, task, dict(leg, result=result), status)

    def finish(s):
        lg = s["legs"][n - 1]
        wait = lg.get("capacity_wait")
        if isinstance(wait, dict) and wait.get("started_at") is not None:
            ended = now()
            lg["capacity_wait_seconds"] = lg.get("capacity_wait_seconds", 0) + max(0.0, ended - wait["started_at"])
            lg.setdefault("capacity_wait_intervals", []).append(
                {"started_at": wait["started_at"], "ended_at": ended})
        lg.update(status=status, executor_status=status, exit_code=rc, ended_at=now(), result=result, end_ref=end_ref,
                  diff=diff, usage=dg["usage"], log_grew=log_size > lg.get("log_size_at_start", 0),
                  stderr_tail=stderr_tail if status == "failed" else "", codex_exited=True, codex_pid=None)
        if notes:
            lg["error"] = "；".join(notes)
        else:
            lg.pop("error", None)
        lg.pop("capacity_wait", None)
        if capacity_final:
            lg["failure_kind"] = "capacity"
            lg["capacity_retry_count"] = capacity_retry_count
        if violation:
            lg["violations"] = violation.get("violations", [])
            lg["monitor_changes"] = violation.get("monitor", [])
            lg["external_freeze_warnings"] = violation.get("external_warn", [])
        lg["verify"] = verify
        if memory_tool_reason:
            lg["memory_tool_unavailable"] = memory_tool_reason
        else:
            lg.pop("memory_tool_unavailable", None)
        if not s.get("thread_id"):
            s["thread_id"] = thread_id_from(lg["events"])
        s["status"] = "done" if status == "done" else "idle"
    project.update_state(task, finish)
    if dossier_id:
        try:
            current = project.load_state(task)
            final_leg = current_leg(current)
            actual_id = build_dossier(project, task, current, final_leg, status)
            if actual_id:
                dossier_id = actual_id
                project.update_state(task, lambda s: current_leg(s).update(
                    dossier={"id": dossier_id, "type": dossier_type_for(status)}))
                run_monitor_lifecycle(project, task, dossier_id, dossier_type_for(status),
                                      config_for_state(project, current), n, status)
                maybe_auto_curation(project, task, status)
        except Exception as monitor_error:  # monitor failure must become an event, not lose the leg result
            _monitor_state_update(project, task, dossier_id, n, "failed", error=str(monitor_error))
    try:
        os.remove(stop_flag)
    except OSError:
        pass
    safe_quota(block=True, notify=True)


def report_baseline(st, leg):
    """End ref of the previous leg that handed in a report (else the task base)."""
    for lg in reversed(st.get("legs", [])[: leg["n"] - 1]):
        if lg.get("status") == "report" and lg.get("end_ref"):
            return lg["end_ref"]
    return st.get("base_ref")


LEAN_HINT = "lean 模式：请把主会话的思考深度设为 medium（conductor.reasoning_effort）。"


def print_lean_hint(project, task=None, st=None):
    if st is None:
        st = project.read_state(task, {}) or {} if task else {}
    state = st
    if config_for_state(project, state).get("mode") == "lean":
        print(LEAN_HINT)


def leg_event_text(project, task, leg):
    st = project.read_state(task, {}) or {}
    cfg = config_for_state(project, st)
    monitor = leg.get("monitor") or {}
    monitor_failed = monitor.get("status") == "failed"
    ev = "MONITOR_FAILED" if monitor_failed else LEG_END_EVENTS.get(leg["status"], str(leg["status"]).upper())
    verify = leg.get("verify") or {}
    first = f"BATON::EVENT {ev} task={task} leg={leg['n']}"
    if verify.get("skipped") and "没有 baton 亲跑的验证" in (verify.get("skip_reason") or ""):
        first += "：本任务没有 baton 亲跑的验证"
    lines = [first]
    if leg.get("model"):
        model_note = f"实际模型：{leg['model']}"
        if leg.get("escalated"):
            model_note += "（升级）"
            if leg.get("escalation_reason"):
                model_note += "；原因：" + str(leg["escalation_reason"])
        lines.append(model_note)
    if cfg.get("mode") == "lean":
        lines.append(LEAN_HINT)
    if st.get("baseline_verify"):
        lines.append("验证基线：" + _verification_summary(st["baseline_verify"]))
    res = leg.get("result") if isinstance(leg.get("result"), dict) else {}
    if res.get("summary"):
        lines.append(f"执行者摘要：{clip(str(res['summary']), 1500)}")
    if res.get("artifact"):
        lines.append(f"产物：{res['artifact']}")
    if leg.get("memory_error"):
        lines.append("记忆工具启动提示：" + str(leg["memory_error"]))
    if leg.get("memory_fallback_reason"):
        lines.append("记忆语义检索退回关键词：" + str(leg["memory_fallback_reason"]))
    if leg.get("memory_tool_unavailable"):
        lines.append("记忆工具：" + str(leg["memory_tool_unavailable"]))
    if leg.get("memory_core_truncated"):
        lines.append("记忆常驻块超过 2,200 字符，已按 pin 顺序截断")
    if leg.get("diff"):
        f, a, d = leg["diff"]
        lines.append(f"本轮改动（不含 .baton/）：{f} 个文件，+{a} −{d}")
        cs = cfg.get("change_size", {})
        if res.get("change_size") == "minor" and (f >= cs["major_files"] or a + d >= cs["major_lines"]):
            lines.append("注意：执行者标为 minor，但改动量超过 major 阈值，可能是未汇报的大修改。")
        if (f or a or d) and not leg.get("log_grew"):
            lines.append("注意：有代码改动但 log.md 本轮没有新增记录。")
    if ev == "REPORT" and leg.get("end_ref"):
        st = project.read_state(task, {}) or {}
        base = report_baseline(st, leg)
        store = project.store(task, st)
        if base and store:
            try:
                f, a, d = shortstat(store, base, leg["end_ref"])
                lines.append(f"自上次汇报以来（{base}）：{f} 个文件，+{a} −{d}；审阅用 baton diff {task} --since report")
            except Exception:  # noqa: BLE001
                pass
    if leg.get("usage"):
        lines.append(f"token：{fmt_usage(leg['usage'])}")
    lines.append(f"用时：{fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}")
    if leg.get("error"):
        lines.append(f"错误：{leg['error']}")
    if monitor.get("id"):
        if monitor.get("status") == "completed":
            lines.append(f"监督简报：{monitor.get('brief_path')}")
            if monitor.get("recommendation"):
                lines.append("建议结论：" + str(monitor["recommendation"]))
            findings = monitor.get("findings")
            if findings is not None:
                lines.append(f"偏离条数：{findings}（blocking={monitor.get('blocking', 0)}，note={monitor.get('note', 0)}）")
            if monitor.get("summary"):
                lines.append("监督摘要：" + clip(monitor["summary"], 600))
        elif monitor_failed:
            lines.append("监督失败：" + str(monitor.get("error") or "未知原因"))
            lines.append(f"原事件摘要：{clip(str((leg.get('result') or {}).get('summary') or ''), 600)}")
    check_monitor = leg.get("monitor_check") or {}
    if check_monitor.get("brief_path"):
        lines.append(f"轮中检查简报：{check_monitor.get('brief_path')}")
        lines.append(f"轮中检查结论：{check_monitor.get('conclusion')}；偏离条数：{check_monitor.get('findings', 0)}")
    if leg.get("failure_kind") == "capacity":
        retries = leg.get("capacity_retries") or []
        count = len(retries)
        consecutive = leg.get("capacity_retry_count", count)
        times = "；".join(
            f"第 {i + 1} 次 {item.get('time') or stamp(item.get('at'))}（{item.get('phase')}）"
            for i, item in enumerate(retries))
        reset = f"；其中最后连续 {consecutive} 次" if consecutive != count else ""
        lines.append(f"容量不足，已自动重试 {count} 次{reset}" + ("；" + times if times else ""))
    if leg.get("escalated") and leg.get("status") in {"blocked", "failed"}:
        lines.append("升级轮未通过，后续不会自动再次升级；请 conductor 决定继续用 Sol、拆分任务或问用户。")
    if leg.get("violations"):
        lines.append("VIOLATION 文件：" + freeze_summary({"violations": leg["violations"]}))
    if leg.get("monitor_changes"):
        lines.append("监看级改动：" + ", ".join(
            f"{x.get('path', '?')}（{','.join(x.get('parts') or ['整文件'])}）"
            for x in leg["monitor_changes"]))
    monitor_verdict = _verdict_for_dossier(project, task, monitor.get("id")) if monitor.get("id") else None
    if monitor_verdict and monitor_verdict.get("conclusion") == "REVISE":
        key = _revision_key(project, monitor.get("id")) or monitor.get("id")
        count = _revision_count(project, task, monitor.get("id"))
        lines.append(f"REVISE 累计：事项 {key} 已有 {count} 份卷宗的最新裁决为 REVISE。")
        if count >= 3:
            lines.append("提示：REVISE 已累计 3 次，conductor 应停下并把卷宗、简报和裁决交给用户。")
        if leg.get("escalated"):
            lines.append("升级轮仍未通过，后续不会自动再次升级；请 conductor 决定继续用 Sol、拆分任务或问用户。")
    if leg.get("external_freeze_warnings"):
        lines.append("WARN：项目外冻结文件变化：" + ", ".join(leg["external_freeze_warnings"]))
    if leg.get("external_actions"):
        lines.append("本轮的外部动作：" + "; ".join(
            json.dumps(action, ensure_ascii=False, sort_keys=True) for action in leg["external_actions"]))
    if leg.get("verify"):
        lines.append("验证摘要：" + _verification_summary(leg["verify"]))
    if leg.get("stderr_tail"):
        lines.append("stderr 末尾：\n" + leg["stderr_tail"])
    lines.append(f"回滚点：{leg.get('start_ref') or '无'} → {leg.get('end_ref') or '无'}")
    q = safe_quota(block=False)
    if codex_quota.is_low(q):
        lines.append(codex_quota.describe(q).splitlines()[0])
    dossier_id = (leg.get("dossier") or {}).get("id") or monitor.get("id")
    if ev in ("REPORT", "DONE", "VIOLATION") and dossier_id:
        next_step = (f"读取监督简报后运行 baton rule {task} {dossier_id} <裁决文件>，"
                     f"再 baton resume {task} --verdict {dossier_id}。")
    elif ev == "BREAKING":
        next_step = f"读取轮中检查简报 {check_monitor.get('brief_path') or '<简报>'}，确认回滚点和验证结果后处理 BREAKING。"
    elif ev == "DRIFT_UNHANDLED":
        next_step = f"读取轮中检查简报 {check_monitor.get('brief_path') or '<简报>'}，先 baton steer {task} 或 dismiss 后再 resume。"
    elif ev == "DECISION":
        artifact = ((leg.get("result") or {}).get("artifact")
                    if isinstance(leg.get("result"), dict) else "") or ""
        match = re.search(r"(?:^|[^A-Za-z])(D\d{3})(?:[^0-9]|$)", str(artifact))
        decision_id = match.group(1) if match else None
        if not decision_id:
            candidates = sorted(re.findall(r"^D\d{3}-", name)[0][:-1]
                                for name in os.listdir(project.p("decisions"))
                                if re.match(r"^D\d{3}-.*\.md$", name)) \
                if os.path.isdir(project.p("decisions")) else []
            decision_id = candidates[-1] if candidates else "<D号>"
        next_step = f"运行 baton decide {task} {decision_id} <决定文件>，再 baton resume {task}。"
    elif ev in ("REPORT", "DONE", "VIOLATION") and not dossier_id:
        next_step = f"审阅执行者汇报，运行 baton diff {task}，然后 baton resume {task}。"
    else:
        next_step = ("可以等待、重跑 monitor 或问用户；不能跳过简报直接裁决。"
                 if ev == "MONITOR_FAILED" else
                 "容量不足的自动重试已用完：等一段时间后 baton resume，或问用户是否换模型；不要接手写代码。"
                 if ev == "FAILED" and leg.get("failure_kind") == "capacity"
                 else NEXT_STEP.get(ev, "查看 baton status"))
    lines.append("下一步：" + next_step)
    return "\n".join(lines)


def cmd_wait(project, args):
    project.require_init()
    reap_detached_runners()
    minutes = args.tick
    last_quota_check = 0.0
    while True:
        st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
        if minutes is None:
            minutes = config_for_state(project, st)["supervision"]["interval_minutes"]
        leg = current_leg(st)
        if not leg:
            print(f"BATON::EVENT IDLE task={args.task}：还没有启动任何一轮")
            print_lean_hint(project, args.task, st)
            reap_detached_runners()
            return
        monitor_running = ((leg.get("monitor") or {}).get("status") == "running"
                           or (leg.get("monitor_check") or {}).get("status") == "running"
                           or (st.get("curation_monitor") or {}).get("status") == "running")
        pending_drift = st.get("pending_drift") or {}
        if pending_drift and not pending_drift.get("event_reported"):
            print(f"BATON::EVENT DRIFT task={args.task} leg={leg['n']} dossier={pending_drift.get('dossier')}")
            print(f"简报：{pending_drift.get('brief_path') or '未知'}；偏离条数：{len(pending_drift.get('finding_ids') or [])}")
            print("摘要：" + clip(pending_drift.get("summary") or "轮中检查发现偏离", 300))
            print("下一步：" + NEXT_STEP["DRIFT"])
            project.update_state(args.task, lambda state: (state.get("pending_drift") or {}).update(event_reported=True))
            reap_detached_runners()
            return
        if leg["status"] != "running" and not monitor_running:
            if leg.get("reported"):
                print(f"BATON::EVENT IDLE task={args.task} leg={leg['n']}：没有运行中的一轮，"
                      f"上一轮状态 {leg['status']}（已处理过）。需要继续就 baton resume。")
                print_lean_hint(project, args.task, st)
                reap_detached_runners()
                return
            print(leg_event_text(project, args.task, leg), flush=True)
            mark_reported(project, args.task)
            reap_detached_runners()
            return
        last_sup = st.get("last_supervision_at", leg["started_at"])
        capacity_retrying = isinstance(leg.get("capacity_wait"), dict)
        elapsed_sup = now() - last_sup - capacity_wait_seconds(leg, since=last_sup)
        cfg = config_for_state(project, st)
        fallback = bool(st.get("supervision_fallback") or (leg.get("supervision_fallback")))
        use_legacy_tick = (not monitor_enabled(cfg) or fallback)
        if not capacity_retrying and use_legacy_tick and supervision_tick_due(elapsed_sup, minutes):
            print(f"BATON::EVENT TICK task={args.task} leg={leg['n']}：距上次监管 {fmt_dur(elapsed_sup)}，"
                  f"本轮已运行 {fmt_dur(now() - leg['started_at'])}")
            print_lean_hint(project, args.task, st)
            print("下一步：" + NEXT_STEP["TICK"])
            reap_detached_runners()
            return
        if not capacity_retrying and now() - last_quota_check > 300:
            last_quota_check = now()
            data = safe_quota(block=True, notify=True)
            keys = codex_quota.low_window_keys(data)
            alerted = st.get("quota_alerted") or []
            fresh = [k for k in keys if not codex_quota.already_alerted(k, alerted)]
            if fresh:
                project.update_state(args.task, lambda s: s.update(quota_alerted=sorted(set((s.get("quota_alerted") or []) + keys))))
                print(f"BATON::EVENT QUOTA_LOW task={args.task} leg={leg['n']}")
                print_lean_hint(project, args.task, st)
                print(codex_quota.describe(data))
                print("下一步：" + NEXT_STEP["QUOTA_LOW"])
                reap_detached_runners()
                return
        time.sleep(10)


def cmd_monitor(project, args):
    project.require_init()
    if getattr(args, "curate", False):
        dossier_id = build_curation_dossier(project, args.task)
        if not dossier_id:
            die("lean 模式或 monitor.enabled=false 不启动整理回合")
        return _start_monitor_process(project, args.task, dossier_id, rerun=bool(getattr(args, "rerun", False)), curation=True)
    st = project.load_state(args.task)
    other = running_task(project, exclude=args.task)
    if other:
        die(f"同一项目里任务 {other[0]} 的第 {other[1]} 轮仍在运行，不能启动 monitor")
    leg = current_leg(st)
    if not leg:
        die("任务还没有轮次，不能运行 monitor")
    if leg.get("status") == "running" and (leg.get("monitor") or {}).get("status") != "running":
        die("执行者轮次仍在运行，等轮次结束后再运行 monitor")
    dossier = dossier_path(project, args.dossier)
    if not os.path.isdir(dossier):
        die(f"找不到卷宗 {args.dossier}")
    if args.rerun and not os.path.isfile(os.path.join(dossier, "manifest.json")):
        # A dossier whose build failed (e.g. invalid memory candidates) is rebuilt from the leg.
        status = leg.get("executor_status") or leg.get("status")
        if dossier_id_for(project, args.task, leg, status) != args.dossier:
            die(f"卷宗 {args.dossier} 缺少 manifest，且不是当前轮次的卷宗，不能重建")
        try:
            rebuilt = build_dossier(project, args.task, st, leg, status)
        except ConfigError as exc:
            die(f"重建卷宗 {args.dossier} 失败：{exc}")
        if rebuilt != args.dossier:
            die(f"重建卷宗 {args.dossier} 失败：得到 {rebuilt}")
        project.update_state(args.task, lambda s: current_leg(s).update(
            dossier={"id": rebuilt, "type": dossier_type_for(status)}))
        print(f"已重建卷宗 {args.dossier}")
    brief_path = project.p("reviews", args.dossier + "-brief.md")
    if os.path.exists(brief_path) and not args.rerun:
        die(f"卷宗 {args.dossier} 已有监督简报；需要重跑请加 --rerun")
    if (leg.get("monitor") or {}).get("status") == "running":
        die("monitor 已在运行，请用 baton wait 等待")
    kind = dossier_manifest(project, args.dossier).get("type") or dossier_type_for(leg.get("executor_status") or leg.get("status"))
    if kind not in MONITOR_RECOMMENDATIONS:
        kind = dossier_type_for(leg.get("executor_status") or leg.get("status"))
    project.update_state(args.task, lambda s: current_leg(s).update(
        monitor={"id": args.dossier, "status": "running", "pid": None, "started_at": now(),
                 "attempt": 0, "rerun": bool(args.rerun),
                 "marker": f"_monitor_run {args.task} {args.dossier}"}, reported=False))
    log_path = os.path.join(dossier_out_dir(project, args.dossier), "rerun.runner.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    try:
        marker = f"_monitor_run {args.task} {args.dossier}"
        runner = platform_support.detached_popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                   "_monitor", "--marker", marker, args.task, args.dossier], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, cwd=project.root)
    finally:
        log.close()
    project.update_state(args.task, lambda s: (current_leg(s).get("monitor") or {}).update(pid=runner.pid,
                                                                                          marker=f"_monitor_run {args.task} {args.dossier}"))
    _DETACHED_RUNNERS.append(runner)
    print(f"已后台启动 monitor：{args.dossier}（pid {runner.pid}）")


def _start_monitor_process(project, task, dossier_id, rerun=False, curation=False):
    """Start a monitor runner for ordinary or task-level curation dossiers."""
    log_path = os.path.join(dossier_out_dir(project, dossier_id), "curation.runner.log" if curation else "rerun.runner.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "w", encoding="utf-8")
    marker = f"_monitor_run {task} {dossier_id}"
    try:
        runner = platform_support.detached_popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                   "_monitor", "--marker", marker, task, dossier_id], stdin=subprocess.DEVNULL,
                                  stdout=log, stderr=log, cwd=project.root)
    finally:
        log.close()
    def update(state):
        if curation:
            state["curation_monitor"] = {"id": dossier_id, "status": "running", "pid": runner.pid,
                                         "started_at": now(), "rerun": rerun, "marker": marker}
        elif current_leg(state):
            current_leg(state)["monitor"] = {"id": dossier_id, "status": "running", "pid": runner.pid,
                                               "started_at": now(), "rerun": rerun, "marker": marker}
    project.update_state(task, update)
    _DETACHED_RUNNERS.append(runner)
    print(f"已后台启动 monitor：{dossier_id}（pid {runner.pid}）")


def maybe_auto_curation(project, task, leg_status):
    st = project.read_state(task, {}) or {}
    cfg = config_for_state(project, st)
    if not monitor_enabled(cfg):
        return None
    if (st.get("curation_monitor") or {}).get("status") == "running":
        return None
    report_count = 0
    base = os.path.join(project.state_dir, "dossiers")
    if os.path.isdir(base):
        for name in os.listdir(base):
            manifest = read_json(os.path.join(base, name, "manifest.json"), {}) if os.path.isdir(os.path.join(base, name)) else {}
            if isinstance(manifest, dict) and manifest.get("type") == "report":
                report_count += 1
    if leg_status != "done" and report_count == 0:
        return None
    if leg_status != "done" and report_count % 3:
        return None
    dossier_id = build_curation_dossier(project, task)
    if dossier_id:
        _start_monitor_process(project, task, dossier_id, curation=True)
    return dossier_id


def cmd_monitor_run(project, args):
    st = project.load_state(args.task)
    cfg = config_for_state(project, st)
    kind = dossier_manifest(project, args.dossier).get("type")
    if not kind and "-curate-" in args.dossier:
        kind = "curation"
    if kind == "curation":
        curation_cfg = dict(cfg)
        curation_cfg["monitor"] = dict(cfg.get("monitor") or {},
                                        model=(cfg.get("memory") or {}).get("curation_model", "sonnet"))
        run_monitor_lifecycle(project, args.task, args.dossier, kind, curation_cfg, None, "curation")
        project.update_state(args.task, lambda s: s.update(curation_monitor={"id": args.dossier,
            "status": "completed", "ended_at": now()}))
        return
    leg = current_leg(st)
    if not leg:
        return
    kind = kind or dossier_type_for(leg.get("executor_status") or leg.get("status"))
    run_monitor_lifecycle(project, args.task, args.dossier, kind, cfg, leg.get("n"),
                          leg.get("executor_status") or leg.get("status"))


def supervision_tick_due(elapsed_seconds, interval_minutes):
    """Whether a wait call should return a periodic TICK event."""
    return float(interval_minutes) > 0 and elapsed_seconds >= float(interval_minutes) * 60


def cmd_status(project, args):
    project.require_init()
    task = args.task
    v1_in_flight = project.legacy_state_in_flight(task)
    st = project.load_state(task)
    if not v1_in_flight:
        st = refresh_runner_liveness(project, task, st)
    cfg = config_for_state(project, st)
    leg = current_leg(st)
    freeze_check_result = check_freeze(project, task, st)
    print(f"# Baton 状态 · {task} · {stamp()}")
    print(f"任务状态：{st.get('status')}；Codex 会话：{st.get('thread_id') or '未知'}；共 {len(st.get('legs', []))} 轮")
    if st.get("status") == "closed":
        closed = st.get("closed") or {}
        if closed.get("abandoned"):
            print(f"任务已按放弃 closed（未终审）：{closed.get('reason')}，账本 seq {closed.get('ledger_seq') or '未知'}")
        else:
            print(f"任务已 closed：终审 {closed.get('dossier') or '未知'}，账本 seq {closed.get('ledger_seq') or '未知'}")
    revision_counts = _revision_counts(project, task)
    if revision_counts:
        print("REVISE 累计：" + "，".join(f"{k}={v}" for k, v in sorted(revision_counts.items())))
    if freeze_check_result.get("violations"):
        print("冻结违规：" + freeze_summary(freeze_check_result))
    if freeze_check_result.get("monitor"):
        print("监看级改动：" + ", ".join(
            f"{x['path']}（{','.join(x.get('parts') or ['整文件'])}）"
            for x in freeze_check_result["monitor"]))
    if freeze_check_result.get("external_warn"):
        print("WARN：项目外冻结文件变化：" + ", ".join(freeze_check_result["external_warn"]))
    if st.get("baseline_verify"):
        print("验证基线：" + _verification_summary(st["baseline_verify"]))
    if leg and leg.get("verify"):
        print("验证摘要：" + _verification_summary(leg["verify"]))
    if not leg:
        return
    running = leg["status"] == "running"
    capacity_wait = leg.get("capacity_wait") if running else None
    status_line = f"当前第 {leg['n']} 轮（{leg['kind']}）：{leg['status']}，已运行 {fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}"
    if running and not isinstance(capacity_wait, dict):
        status_line += f"，codex pid {leg.get('codex_pid')} {'存活' if codex_alive(leg) else '不在'}"
    print(status_line)
    if isinstance(capacity_wait, dict):
        retry_no = capacity_wait.get("retry", "?")
        if capacity_wait.get("phase") == "delayed":
            remaining = max(0.0, float(capacity_wait.get("next_retry_at", now())) - now())
            minutes_left = max(1, int(math.ceil(remaining / 60)))
            print(f"容量不足，{minutes_left} 分钟后重试（第 {retry_no} 次）")
        else:
            print(f"容量不足，正在重试（第 {retry_no} 次）")
    alerts = leg.get("alerts") or []
    if alerts:
        print("本轮 ALERT：")
        for alert in alerts:
            print(f"- {alert.get('id')} · {alert.get('rule')} · {alert.get('category')}：{alert.get('evidence')} @ {stamp(alert.get('at'))}")
    elif cfg.get("mode") != "lean" and monitor_enabled(cfg):
        print("本轮 ALERT：无")
    if monitor_enabled(cfg):
        last_check = leg.get("last_check") or {}
        if last_check:
            print(f"最近轮中检查：{last_check.get('dossier')} · {last_check.get('conclusion')} · {stamp(last_check.get('at'))}")
        next_check = leg.get("next_check_at")
        if next_check:
            interval = leg.get("check_interval_minutes")
            print(f"下一次检查：{stamp(next_check)}（当前间隔 {interval} 分钟）")
        print("轮中检查兜底：" + ("是" if st.get("supervision_fallback") else "否"))
        pending = st.get("pending_drift") or {}
        if pending:
            remaining = max(0.0, float(pending.get("deadline", now())) - now())
            print(f"pending_drift：{pending.get('dossier')}，剩余处置 {fmt_dur(remaining)}，F={','.join(pending.get('finding_ids') or [])}")
    monitor = leg.get("monitor") or {}
    check_monitor = leg.get("monitor_check") or {}
    if check_monitor:
        if check_monitor.get("status") == "running":
            print(f"轮中检查：运行中（卷宗 {check_monitor.get('id')}）")
        elif check_monitor.get("status") == "completed":
            print(f"轮中检查：{check_monitor.get('conclusion')}（卷宗 {check_monitor.get('id')}）")
        elif check_monitor.get("status") == "failed":
            print(f"轮中检查：失败，{check_monitor.get('error') or '未知原因'}")
    if monitor:
        mstatus = monitor.get("status") or "unknown"
        if mstatus == "running":
            print(f"monitor：运行中（{fmt_dur(now() - float(monitor.get('started_at', now())))}），卷宗 {monitor.get('id')}")
        elif mstatus == "completed":
            print(f"monitor：完成，监督简报 {monitor.get('brief_path')}")
        elif mstatus == "failed":
            print(f"monitor：失败，{monitor.get('error') or '未知原因'}；卷宗 {monitor.get('id')}，可用 --rerun 重跑")
        verdict = _verdict_for_dossier(project, task, monitor.get("id")) if monitor.get("id") else None
        if verdict:
            print(f"当前卷宗裁决：已裁决 {verdict.get('conclusion')}（ledger seq {verdict.get('seq')}）")
        elif monitor.get("id") and mstatus == "completed":
            print("当前卷宗裁决：未裁决")
        if isinstance(monitor.get("capacity_wait"), dict):
            wait = monitor["capacity_wait"]
            remaining = max(0.0, float(wait.get("next_retry_at", now())) - now())
            print(f"monitor 容量不足，{max(1, int(math.ceil(remaining / 60)))} 分钟后重试（第 {wait.get('retry', '?')} 次）")
    if leg["status"] != "running" and not leg.get("reported"):
        print(f"注意：这一轮已结束（{leg['status']}），事件还没被 baton wait 报告过。")
    last_sup = st.get("last_supervision_at", leg["started_at"])
    sup_elapsed = max(0.0, now() - last_sup - capacity_wait_seconds(leg, since=last_sup))
    print(f"上次监管：{stamp(last_sup)}（{fmt_dur(sup_elapsed)} 前）；"
          f"自动暂停时限 {cfg['supervision'].get('max_unsupervised_minutes')} 分钟")
    print(codex_quota.describe(safe_quota(block=True)))

    sup = st.get("supervision") or {}
    same_leg = sup.get("leg") == leg["n"]
    offset = sup.get("events_offset", 0) if same_leg else 0
    events, events_end = read_events(leg["events"], offset)
    dg = digest(events)
    still_running = digest(read_events(leg["events"], 0)[0])["running"] if running else []
    print(f"\n## 上次监管以来的执行者活动（{len(events)} 个事件）")
    if still_running:
        print("正在执行：" + "；".join(clip(c, 200) for c in still_running))
    if dg["commands"]:
        failed = sum(1 for _, code in dg["commands"] if code not in (0, None))
        print(f"命令 {len(dg['commands'])} 条（非零退出 {failed} 条），最近 {min(15, len(dg['commands']))} 条：")
        for c, code in dg["commands"][-15:]:
            print(f"  [{code}] {clip(c, 220)}")
    if dg["files"]:
        print(f"文件变更 {len(dg['files'])} 个：")
        for path, kind in list(dg["files"].items())[-40:]:
            print(f"  {kind:<7} {os.path.relpath(path, project.root) if os.path.isabs(path) else path}")
    for m in dg["messages"][-3:]:
        print("执行者消息：" + clip(m, 600))
    for e in dg["errors"]:
        print("错误：" + clip(e, 400))

    now_commit = None
    store = project.store(task, st)
    if store:
        print("\n## 代码改动（不含 .baton/ .council/）")
        try:
            now_commit = store.snapshot("baton status probe")
        except Exception as e:  # noqa: BLE001
            print(f"（快照失败，无法统计改动：{e}）")
    if now_commit:
        for label, ref in (("任务起点", st.get("base_ref")), ("本轮起点", leg.get("start_ref")),
                           ("上次监管", sup.get("ref"))):
            if ref:
                f, a, d = shortstat(store, ref, now_commit)
                print(f"相对{label} {ref}：{f} 个文件，+{a} −{d}")
        if leg.get("start_ref"):
            stat_out = store.git("diff", "--stat=120", leg["start_ref"], now_commit, "--", ".", *code_excludes(store), check=False)
            print(stat_out.rstrip()[-4000:] or "（本轮暂无改动）")
            pairs = [(s, p) for _, _, s, p in diff_tree_entries(store, leg["start_ref"], now_commit)]
            tracked = store.user_tracked() if store.kind == "shadow" else set()
            deleted_paths = [p for s, p in pairs if s == "D"]
            excluded_set = shadow_excluded_many(store, deleted_paths, tracked)
            deleted = [p for p in deleted_paths if p not in excluded_set]
            excluded_deleted = [p for p in deleted_paths if p in excluded_set]
            if deleted:
                print(f"注意：本轮删除了 {len(deleted)} 个文件（含改名前的旧路径）：" + "，".join(deleted[:20]))
            if excluded_deleted:
                print("注意：以下路径当前被影子仓库排除，相对旧回滚点显示为删除："
                      + "，".join(excluded_deleted[:20]))
            risky = [p for _, p in pairs if re.search(r"(^|/)(\.github|\.gitlab-ci|Dockerfile|pyproject\.toml|package(-lock)?\.json|requirements.*\.txt|setup\.(py|cfg)|go\.mod|Cargo\.toml)", p)]
            if risky:
                print("注意：改到了构建/依赖/CI 文件：" + "，".join(risky[:20]))
            tests = [p for s, p in pairs if s in ("D", "M", "T") and re.search(r"(test|spec)", p, re.I)]
            if tests:
                print("注意：改动或删除了测试文件：" + "，".join(tests[:20]))

    log_path = project.p("log.md")
    log_off = sup.get("log_offset") if same_leg and sup.get("log_offset") is not None else leg.get("log_size_at_start", 0)
    new_log = read_bytes_since(log_path, log_off)
    log_end = file_size(log_path)
    print("\n## log.md 新增")
    if new_log is None:
        print("注意：log.md 被删除或比上次监管时变短了，执行者可能改写了已有记录（协议只允许追加）。")
    elif len(new_log) > 4000:
        heads = [l for l in new_log.splitlines() if l.startswith("## ")]
        print(f"新增 {len(heads)} 条记录（{len(new_log)} 字），标题如下，最后部分全文附后：")
        for h in heads:
            print("  " + h)
        print("……\n" + new_log[-3000:])
    else:
        print(new_log.strip() or "（无新增）")
    since = sup.get("since", last_sup)
    for sub in ("reports", "decisions"):
        fresh = [f for f in sorted(os.listdir(project.p(sub))) if os.path.getmtime(project.p(sub, f)) > since] \
            if os.path.isdir(project.p(sub)) else []
        if fresh:
            print(f"\n{sub}/ 新文件：" + "，".join(fresh))
    tc = cfg["supervision"].get("test_command")
    print(f"\n验证命令：{tc}" if tc else "\n验证命令：未配置（.baton/config.json → supervision.test_command）")

    if v1_in_flight:
        print("提示：Baton v1 这一轮仍在运行，未写入 pending_supervision；等它结束后再用 v2 命令监管。")
        return

    pend = {"leg": leg["n"], "events_offset": events_end, "log_offset": log_end,
            "commit": now_commit, "at": now()}
    project.update_state(task, lambda s: s.update(pending_supervision=pend))


def _final_dossiers(project, task):
    base = os.path.join(project.state_dir, "dossiers")
    found = []
    prefix = task + "-final"
    if not os.path.isdir(base):
        return found
    for name in os.listdir(base):
        if name == prefix:
            number = 1
        else:
            match = re.fullmatch(re.escape(prefix) + r"-(\d+)", name)
            if not match:
                continue
            number = int(match.group(1))
        if os.path.isdir(os.path.join(base, name)):
            found.append((number, name))
    return [name for _, name in sorted(found)]


def _close_rejected_findings(project, task, records):
    briefs = {r.get("seq"): r for r in records if r.get("type") == "brief" and r.get("task") == task}
    latest = {}
    for record in records:
        if record.get("type") == "verdict" and record.get("task") == task:
            latest[record.get("dossier")] = record
    rows = []
    for dossier, verdict in latest.items():
        brief = briefs.get(verdict.get("brief_seq"))
        metadata = _brief_findings((brief or {}).get("brief", ""), (brief or {}).get("brief_json"))
        for fid, response in (verdict.get("findings") or {}).items():
            if response.get("decision") == "reject":
                meta = metadata.get(fid, {})
                rows.append((dossier, fid, meta.get("category", "unknown"), meta.get("severity", "unknown"),
                             response.get("reason", "")))
    return rows


def cmd_close(project, args):
    project.require_init()
    task = args.task
    st = project.load_state(task)
    if st.get("status") == "closed":
        die(f"任务 {task} 已 closed")
    for leg in st.get("legs", []):
        monitor_value = leg.get("monitor") or {}
        curation_monitor = str(monitor_value.get("id", "")).startswith(task + "-curate-")
        if leg.get("status") == "running" or (monitor_value.get("status") == "running" and not curation_monitor):
            die("还有运行中的轮次或 monitor，不能 close（leg %s status=%s monitor=%s）" %
                (leg.get("n"), leg.get("status"), json.dumps(monitor_value, ensure_ascii=False)))
    if not verify_ledger(project):
        die("账本校验失败，不能 close")
    reason = getattr(args, "abandon", None)
    if reason is not None:
        if not reason.strip():
            die("--abandon 需要写明放弃原因")
        record = append_ledger(project, {"type": "abandon", "task": task, "reason": reason.strip()})
        project.update_state(task, lambda s: s.update(status="closed", closed={"abandoned": True,
            "reason": reason.strip(), "ledger_seq": record.get("seq"), "at": now()}))
        prune_closed_task(project, task)
        print(f"任务 {task} 已按放弃 closed（未终审，账本 seq {record.get('seq')}）：{reason.strip()}")
        return
    dossiers = _final_dossiers(project, task)
    if not dossiers:
        die("没有终审卷宗，不能 close")
    dossier_id = dossiers[-1]
    good, error = verify_dossier(project, dossier_id)
    if not good:
        die("终审卷宗校验失败：" + error)
    manifest = dossier_manifest(project, dossier_id)
    brief, dossier_hash = _dossier_brief(project, task, dossier_id, manifest)
    if not brief:
        die("最新终审卷宗没有绑定简报，不能 close")
    verdict = _latest_ledger_record(ledger_records(project), "verdict", task, dossier=dossier_id)
    if not verdict or verdict.get("conclusion") != "ACCEPT" or verdict.get("dossier_hash") != dossier_hash \
            or verdict.get("brief_seq") != brief.get("seq") \
            or verdict.get("brief_record_hash") != brief.get("record_hash"):
        die("最新终审卷宗没有绑定最新简报的 ACCEPT 裁决，不能 close")
    leg = _dossier_leg(project, task, dossier_id, st)
    if not leg:
        die("找不到终审卷宗所在轮次，不能 close")
    verify = leg.get("verify") or read_json(project.leg_path(task, leg.get("n"), "verify.json"), {}) or {}
    if verify.get("skipped"):
        if "没有 baton 亲跑" not in (verdict.get("rendered") or verdict.get("verdict") or ""):
            die("终审没有亲跑验证，且 ACCEPT 裁决没有明确写明这一点")
        print("提示：本任务没有 baton 亲跑验证，close 依据裁决中的明确说明。")
    elif verify.get("exit_code") != 0 or verify.get("acceptance_failed"):
        die("终审轮次验收测试未全部通过，不能 close")
    store = project.store(task, st)
    if not store or not leg.get("end_ref"):
        die("终审卷宗缺少终点快照，不能 close")
    try:
        current_ref = store.snapshot("baton close probe")
    except Exception as exc:
        die(f"重新快照失败，不能 close：{exc}")
    try:
        changed = store.git("diff", "--name-only", leg["end_ref"], current_ref, "--", ".", *code_excludes(store), check=True).strip()
    except RuntimeError as exc:
        die(f"终点快照比较失败，不能 close：{exc}")
    if changed:
        die("ACCEPT 之后代码又变过，当前快照与终审终点不一致，不能 close")
    record = append_ledger(project, {"type": "close", "task": task, "dossier": dossier_id,
                                     "dossier_hash": dossier_hash, "verdict_seq": verdict.get("seq")})
    rows = _close_rejected_findings(project, task, ledger_records(project))
    project.update_state(task, lambda s: s.update(status="closed", closed={"dossier": dossier_id,
        "verdict_seq": verdict.get("seq"), "ledger_seq": record.get("seq"), "at": now()}))
    prune_closed_task(project, task)
    print(f"任务 {task} 已 closed（终审 {dossier_id}，ACCEPT seq {verdict.get('seq')}）。")
    print("整个任务被驳回的偏离：")
    if rows:
        for dossier, fid, category, severity, reason in rows:
            print(f"- {dossier} {fid} · {category} · {severity}：{reason}")
    else:
        print("无")


def cmd_supervised(project, args):
    project.require_init()
    task = args.task
    if project.legacy_state_in_flight(task):
        die("任务还有 Baton v1 的一轮在运行，暂不写监管记录；等这一轮结束后再操作")
    st = project.load_state(task)
    leg = current_leg(st)
    dismiss_ids = list(getattr(args, "dismiss", None) or [])
    if dismiss_ids:
        pending = st.get("pending_drift") or {}
        known = set(pending.get("finding_ids") or [])
        unknown = sorted(set(dismiss_ids) - known)
        if unknown:
            die("不能驳回不在 pending_drift 中的 finding：" + ", ".join(unknown))
        reason = (getattr(args, "reason", None) or getattr(args, "note", None) or "").strip()
        if not reason:
            die("--dismiss 需要 --reason")
        for finding_id in dismiss_ids:
            append_ledger(project, {"type": "dismiss", "task": task, "dossier": pending.get("dossier"),
                                    "finding": finding_id, "reason": reason})
        remaining = [item for item in pending.get("finding_ids") or [] if item not in set(dismiss_ids)]
        def dismiss_update(state):
            current = state.get("pending_drift") or {}
            if remaining:
                current["finding_ids"] = remaining
                current["event_reported"] = True
                state["pending_drift"] = current
            else:
                state["pending_drift"] = None
                current_leg(state) and current_leg(state).pop("pending_drift", None)
        project.update_state(task, dismiss_update)
        print("已驳回：" + ", ".join(dismiss_ids) + ("；仍待处理：" + ", ".join(remaining) if remaining else "；pending_drift 已清空"))
        return
    if not args.verdict or not args.note:
        die("监管记录需要 --verdict 和 --note，或使用 --dismiss F1 --reason \"…\"")
    pend = st.get("pending_supervision")
    # Use the baseline of the snapshot Opus actually read, so nothing between status and now is skipped.
    if not (pend and leg and pend.get("leg") == leg["n"] and now() - pend.get("at", 0) < 3600):
        pend = None
    label = f"tick-{stamp(fmt='%Y%m%d-%H%M%S')}"
    ref, err = None, None
    store = project.store(task, st)
    if pend and pend.get("commit") and store:
        try:
            ref = store_ref(store, task, label, pend["commit"])
        except Exception as e:  # noqa: BLE001
            err = str(e)
    else:
        ref, err = try_checkpoint(project, task, label, st=st)
    events_off = pend["events_offset"] if pend else (read_events(leg["events"])[1] if leg else 0)
    log_off = pend["log_offset"] if pend else file_size(project.p("log.md"))
    since = pend["at"] if pend else now()
    with open(project.p("supervision.md"), "a", encoding="utf-8") as f:
        f.write(f"\n## {stamp()} · {task} · 第 {leg['n'] if leg else '-'} 轮 · {args.verdict}\n\n"
                f"{args.note.strip()}\n\n- 回滚点：`{ref or '无'}`\n")

    def mark(s):
        s["last_supervision_at"] = now()
        s["supervision"] = {"leg": leg["n"] if leg else 0, "events_offset": events_off, "log_offset": log_off,
                            "ref": ref or (s.get("supervision") or {}).get("ref"), "since": since}
        s.pop("pending_supervision", None)
    project.update_state(task, mark)
    if err:
        print(f"注意：监管回滚点没建成：{err}")
    print(f"已记录监管结论 {args.verdict}；回滚点 {ref or '无'}。若本轮仍在运行，重新后台 baton wait {task}。")


def cmd_stop(project, args):
    project.require_init()
    before = current_leg(project.load_state(args.task))
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    leg = current_leg(st)
    if leg and (leg.get("monitor") or {}).get("status") == "running" and leg.get("status") != "running":
        with open(project.leg_path(args.task, leg["n"], "stop"), "w", encoding="utf-8") as stream:
            stream.write(args.reason or "stopped by conductor")
        print(f"已请求停止 monitor（卷宗 {(leg.get('monitor') or {}).get('id')}）")
        return
    if not leg or leg["status"] != "running":
        if before and before.get("status") == "running" and leg and leg.get("status") == "failed":
            print(f"第 {leg['n']} 轮的 runner 已不在：{leg.get('error')}")
        elif leg and kill_codex(leg):
            print(f"第 {leg['n']} 轮的 runner 已不在，残留的 codex 进程已终止")
        elif leg and not leg.get("reported") and leg["status"] != "stopped":
            print(leg_event_text(project, args.task, leg))
            mark_reported(project, args.task)
            print(f"第 {leg['n']} 轮在停止前已经自己结束，上面是它的事件。")
        else:
            print("没有运行中的一轮")
        return
    with open(project.leg_path(args.task, leg["n"], "stop"), "w", encoding="utf-8") as f:
        f.write(args.reason or "stopped by conductor")
    deadline = now() + 60
    while now() < deadline:
        time.sleep(2)
        st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
        if current_leg(st)["status"] != "running":
            print(f"第 {leg['n']} 轮已结束：{current_leg(st)['status']}；回滚点 {current_leg(st).get('end_ref')}")
            return
    print("停止信号已发出但 60 秒内未结束；稍后用 baton status 确认")


def cmd_steer(project, args):
    project.require_init()
    message = message_from(args)  # validate before touching the running leg
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    if st.get("status") == "closed":
        die(f"任务 {args.task} 已 closed，不能 steer")
    leg = current_leg(st)
    _resume_ruling_gate(project, args.task, st, action="steer")
    if leg and leg["status"] == "running":
        cmd_stop(project, argparse.Namespace(task=args.task, reason="steer"))
        st = project.load_state(args.task)
        leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("当前轮还没停下，稍后用 baton status 确认后再纠偏")
    _resume_ruling_gate(project, args.task, st, action="steer")
    if leg and not leg.get("reported"):
        if leg["status"] != "stopped":
            surface_unreported(project, args.task, st, "纠偏")
        mark_reported(project, args.task)
    if st.get("pending_drift"):
        def clear_drift(state):
            state["pending_drift"] = None
            if current_leg(state):
                current_leg(state).pop("pending_drift", None)
        project.update_state(args.task, clear_drift)
    archive_conductor_message(project, args.task, message, "纠偏")
    cmd_resume(project, argparse.Namespace(task=args.task, kind="纠偏", _message_archived=True), message=message)


def cmd_checkpoint(project, args):
    check_task(args.task)
    if not project.task_exists(args.task):
        die(f"任务 {args.task} 不存在（没有简报或状态文件）")
    if project.legacy_state_in_flight(args.task):
        die("任务还有 Baton v1 的一轮在运行，暂不建立回滚点；等这一轮结束后再操作")
    label = args.label or f"manual-{stamp(fmt='%Y%m%d-%H%M%S')}"
    if not LABEL_RE.fullmatch(label):
        die("标签只能用字母、数字、下划线和连字符")
    ref, err = try_checkpoint(project, args.task, label)
    if err:
        die(f"回滚点没建成：{err}")
    print(ref)


def cmd_refs(project, args):
    store = project.store(args.task)
    out = store.git("for-each-ref", "--sort=creatordate",
                    "--format=%(creatordate:format:%m-%d %H:%M)  %(refname)", f"refs/baton/{args.task}",
                    check=False) if store and (store.kind == "repo" or os.path.isdir(store.git_dir)) else ""
    visible = [line for line in out.splitlines() if not line.rstrip().endswith("/probe")]
    print("\n".join(visible).rstrip() or "（无回滚点）")


def cmd_diff(project, args):
    st = project.load_state(args.task)
    leg = current_leg(st)
    if str(args.since).rstrip("/").endswith("/probe"):
        die("probe 快照只供轮中规则使用，不是可审阅或回滚的回滚点")
    base = {"base": st.get("base_ref"), "leg": leg and leg.get("start_ref"),
            "tick": (st.get("supervision") or {}).get("ref"),
            "report": leg and report_baseline(st, leg) if leg else None}.get(args.since, args.since)
    if not base:
        die("没有对应的回滚点")
    store = project.store(args.task, st)
    if store is None:
        die("这个任务建立于 Baton v1，项目又不是 git 仓库，没有回滚点")
    if not store.has_commit(base):
        die(f"找不到回滚点 {base}；可用的回滚点见 baton refs {args.task}")
    try:
        target = store.snapshot("baton diff probe")
    except Exception as e:  # noqa: BLE001
        die(f"快照失败：{e}")
    extra = ["--stat=120"] if args.stat else []
    output = store.git("diff", *extra, base, target, "--", ".", *code_excludes(store), check=False)
    sys.stdout.write(output)
    tracked = store.user_tracked() if store.kind == "shadow" else set()
    deleted_paths = [path for _, _, status_, path in diff_tree_entries(store, base, target) if status_ == "D"]
    excluded_set = shadow_excluded_many(store, deleted_paths, tracked)
    excluded = [path for path in deleted_paths if path in excluded_set]
    if excluded:
        if output and not output.endswith("\n"):
            print()
        print("注意：以下路径当前被影子仓库排除，相对旧回滚点显示为删除，diff 不代表会被 rollback 写回："
              + "，".join(excluded[:20]))


# ---------------------------------------------------------------- rollback

def _leading_symlink(top, rel):
    """First leading directory component of rel that is a symlink in the worktree, or None."""
    cur = top
    for part in rel.split("/")[:-1]:
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            return cur
        if not os.path.isdir(cur):
            return None
    return None


def _under(path, prefixes):
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _backup_destination(backup_dir, rel, manifest=None):
    """Choose a backup path, preserving case-only collisions with a manifest entry."""
    dest = os.path.join(backup_dir, rel)
    if os.path.lexists(dest):
        parent, name = os.path.dirname(dest), os.path.basename(dest)
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        exact = name in names
        case_conflict = (not exact and any(n.casefold() == name.casefold() for n in names))
        if not case_conflict:
            raise RuntimeError(f"备份目标已存在：{dest}")
        stem = dest
        n = 2
        while os.path.lexists(dest):
            dest = f"{stem}.{n}"
            n += 1
        if manifest is not None:
            manifest.append({"original": rel.replace(os.sep, "/"),
                             "backup": os.path.relpath(dest, backup_dir).replace(os.sep, "/")})
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    return dest


def _backup(top, full, backup_dir, manifest=None):
    rel = os.path.relpath(full, top)
    dest = _backup_destination(backup_dir, rel, manifest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.islink(full) or not os.path.isdir(full):
        shutil.copy2(full, dest, follow_symlinks=False)
    else:
        shutil.copytree(full, dest, symlinks=True)
    return dest


def _prune_empty_dirs(path, stop):
    stop = os.path.realpath(stop)
    path = os.path.realpath(path)
    while path.startswith(stop + os.sep) and os.path.isdir(path) and not os.path.islink(path):
        try:
            os.rmdir(path)
        except OSError:
            return
        path = os.path.dirname(path)


def _suspend_memory_after_rollback(project, task, store, target_ref, rollback_ref):
    from baton_memory.ledger import MemoryLedger
    ledger = MemoryLedger(project)
    suspended = []
    for ident, record in ledger.latest().items():
        if record.get("status", "active") != "active" or not record.get("end_snapshot"):
            continue
        source = str(record.get("end_snapshot"))
        source_commit = store.git("rev-parse", "--verify", "-q", source + "^{commit}", check=False).strip()
        if not source_commit or source_commit == target_ref:
            continue
        # git returns no stdout for this plumbing command; use the process
        # directly so a missing ref and a false ancestor are distinguishable.
        proc = subprocess.run([platform_support.executable("git"), "-C", store.top, "merge-base", "--is-ancestor", target_ref, source_commit],
                              env=store.env(), capture_output=True)
        if proc.returncode == 0:
            ledger.append_status(ident, "suspended", status_reason="来源快照晚于回滚目标",
                                 rollback_target=target_ref, rollback_ref=rollback_ref)
            suspended.append(ident)
    return suspended


def cmd_rollback(project, args):
    """Restore worktree files under the project root to a checkpoint.

    Only worktree files change: HEAD, branches and the real index stay as they are. The current
    state is checkpointed first, and every file that will be overwritten or deleted is copied
    byte for byte to <state dir>/rollback-backup/<name>/ before anything changes. Nothing is
    written or deleted through a symlinked directory or inside a submodule.
    """
    if str(args.ref).rstrip("/").endswith("/probe"):
        die("probe 快照只供轮中规则使用，不是回滚点")
    root = project.root
    st = project.load_state(args.task)
    store = project.store(args.task, st)
    if store is None:
        die("这个任务建立于 Baton v1，项目又不是 git 仓库，无法回滚")
    target = store.git("rev-parse", "--verify", "-q", f"{args.ref}^{{commit}}", check=False).strip() \
        if store.kind == "repo" or os.path.isdir(store.git_dir) else ""
    if not target:
        die(f"找不到回滚点 {args.ref}")
    leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("执行者仍在运行，先 baton stop")
    if not args.yes:
        print(f"将把 {root} 下的工作区文件（不含 .baton/ .council/）恢复到 {args.ref}。HEAD、分支和暂存区不变。")
        print(f"当前状态会先存成回滚点，要改写或删除的文件会先原样备份到 {os.path.join(project.state_dir, 'rollback-backup')}。子模块内部不在回滚范围内。")
        print("确认后加 --yes 重新执行。")
        sys.exit(3)

    write_baton_gitignore(project)
    top = store.top
    pre_ref, err = try_checkpoint(project, args.task, f"pre-rollback-{stamp(fmt='%Y%m%d-%H%M%S')}", st=st)
    if err:
        die(f"回滚前的状态没能存下来，已放弃回滚：{err}")
    pre = store.git("rev-parse", pre_ref).strip()
    backup_dir = os.path.join(project.state_dir, "rollback-backup", os.path.basename(pre_ref))
    entries = diff_tree_entries(store, pre, target)
    gitlinks = set()
    for tree in (pre, target):
        for line in store.git("ls-tree", "-r", "-z", tree, cwd=top).split("\0"):
            if line.startswith("160000 "):
                gitlinks.add(line.split("\t", 1)[1])
    nested_cache = {}

    def nested_owner(path):
        """The nested project (its own baton.json or .baton/) that owns path, if any."""
        d = os.path.dirname(os.path.join(top, path))
        chain = []
        while d.startswith(root + os.sep):
            if d in nested_cache:
                found = nested_cache[d]
                break
            chain.append(d)
            if is_project_dir(d):
                found = d
                break
            d = os.path.dirname(d)
        else:
            found = None
        for c in chain:
            nested_cache[c] = found
        return found

    tracked = store.user_tracked() if store.kind == "shadow" else set()
    exclusion_candidates = []
    for src, dst, status_, path in entries:
        if status_ not in ("A", "M", "T") or "160000" in (src, dst) or _under(path, gitlinks):
            continue
        owner = nested_owner(path)
        if not owner and not _leading_symlink(top, path):
            exclusion_candidates.append(path)
    excluded_set = shadow_excluded_many(store, exclusion_candidates, tracked)
    deletes, writes, skipped = [], [], []
    for src, dst, status_, path in entries:
        owner = nested_owner(path)
        if "160000" in (src, dst) or _under(path, gitlinks):
            skipped.append(f"{path}（子模块）")
        elif owner:
            skipped.append(f"{path}（属于嵌套项目 {os.path.relpath(owner, root)}）")
        elif _leading_symlink(top, path):
            skipped.append(f"{path}（路径经过符号链接）")
        elif status_ == "D":
            deletes.append(path)
        elif status_ in ("A", "M", "T"):
            if path in excluded_set:
                skipped.append(f"{path}（已排除）")
            else:
                writes.append(path)

    # 0. back up every existing path that will be deleted or overwritten, before changing anything
    backed, seen, backup_manifest = [], set(), []
    try:
        for path in deletes + writes:
            full = os.path.join(top, path)
            if os.path.lexists(full) and not (os.path.isdir(full) and not os.path.islink(full)):
                st_ = os.lstat(full)
                if (st_.st_dev, st_.st_ino) in seen:
                    continue  # same file under another name (case-insensitive disk)
                seen.add((st_.st_dev, st_.st_ino))
                backed.append(_backup(top, full, backup_dir, backup_manifest))
    except Exception as e:  # noqa: BLE001
        die(f"备份要改动的文件时出错，已放弃回滚，工作区没有改动：{e}")

    deleted, written = [], []
    try:
        # 1. deletions first, so a case-only rename on a case-insensitive disk ends with the target file
        for path in deletes:
            full = os.path.join(top, path)
            if os.path.lexists(full) and (os.path.islink(full) or not os.path.isdir(full)):
                os.unlink(full)
                deleted.append(path)
                _prune_empty_dirs(os.path.dirname(full), root)
        # 2. clear what stands in the way of the target files (moved into the backup, never lost)
        for path in writes:
            parts = path.split("/")
            for i in range(1, len(parts)):
                full_lead = os.path.join(top, "/".join(parts[:i]))
                if os.path.islink(full_lead) or (os.path.lexists(full_lead) and not os.path.isdir(full_lead)):
                    backed.append(_backup(top, full_lead, backup_dir, backup_manifest))
                    os.unlink(full_lead)
                    break
            full = os.path.join(top, path)
            if os.path.isdir(full) and not os.path.islink(full):
                if os.path.lexists(os.path.join(full, ".git")):
                    raise RuntimeError(f"{path} 是一个嵌套的 git 仓库，不会移动它")
                dest = _backup_destination(backup_dir, path, backup_manifest)
                shutil.move(full, dest)
                backed.append(dest)
        # 3. write the target versions through a throwaway index (git applies modes and filters);
        #    .gitattributes first so the other files are converted with the target's attributes
        if writes:
            fd, tmp_index = tempfile.mkstemp(prefix="baton-index-")
            os.close(fd)
            os.remove(tmp_index)
            try:
                env = {"GIT_INDEX_FILE": tmp_index}
                store.git("read-tree", target, env=env, cwd=top)
                attrs = [p for p in writes if p.split("/")[-1] == ".gitattributes"]
                rest = [p for p in writes if p not in attrs]
                for batch in (attrs, rest):
                    if batch:
                        store.git("checkout-index", "-f", "-z", "--stdin", env=env, cwd=top,
                            input_text="".join(p + "\0" for p in batch))
            finally:
                if os.path.exists(tmp_index):
                    os.remove(tmp_index)
            written = writes
        if backup_manifest:
            write_json(os.path.join(backup_dir, "manifest.json"), {"case_conflicts": backup_manifest})
    except Exception as e:  # noqa: BLE001
        msg = f"回滚中途出错：{e}\n回滚前的状态在 {pre_ref}，可以用 baton rollback {args.task} {pre_ref} --yes 恢复。"
        if backed:
            msg += f"\n改动前的文件原样备份在 {backup_dir}（{len(backed)} 项），其中被 git 忽略的文件只在这里有副本。"
        die(msg)
    print(f"已把工作区恢复到 {args.ref}：写回 {len(written)} 个文件，删除 {len(deleted)} 个。回滚前状态：{pre_ref}。")
    if backed:
        print(f"改动前的文件原样备份在 {backup_dir}（{len(backed)} 项）。")
    if skipped:
        print("未处理：" + "，".join(skipped[:20]))
    nested = nested_project_dirs_for_code(root)
    if nested and not any("属于嵌套项目" in item for item in skipped):
        print(f"另有 {len(nested)} 个嵌套项目，回滚不涉及（属于嵌套项目 " + "、".join(nested) + "）")
    if gitlinks:
        print("注意：项目里有子模块或嵌套仓库，它们内部的改动不在回滚范围内。")
    staged = [x for x in git(root, "diff", "--cached", "--name-only", "-z", "--", ".", *EXCLUDE,
                             check=False).split("\0") if x] if is_git(root) else []
    if staged:
        print("注意：暂存区里还有这些改动（回滚不碰暂存区）：" + "，".join(staged[:20])
              + "。如需丢弃，问过用户后运行 git restore --staged -- <路径>")
    suspended = _suspend_memory_after_rollback(project, args.task, store, target, pre_ref)
    if suspended:
        print("已挂起来源快照晚于回滚目标的记忆：" + ", ".join(suspended))


def cmd_list(project, args):
    project.require_init()
    for task in project.tasks():
        st = project.read_state(task, {}) or {}
        leg = current_leg(st) if st else None
        print(f"{task:<24} {st.get('status', 'draft'):<8} 轮次 {len(st.get('legs', []))}"
              + (f"  最新：{leg['status']} @ {stamp(leg['started_at'])}" if leg else ""))


def cmd_config(project, args):
    cfg = project.config
    conductor = cfg["conductor"]
    monitor = cfg["monitor"]
    ex = cfg["executor"]
    retry = capacity_retry_config(cfg)
    print(f"BATON::CONFIG judge={conductor['model']} executor={ex['model']} effort={ex['reasoning_effort']}"
          f" conductor={conductor['model']} backend={ex['backend']} project_id={project.project_id} monitor={monitor['model']}"
          f" mode={cfg['mode']}")
    if not args.json:
        print(f"容量重试：immediate={retry['immediate']}，delayed={retry['delayed']}，delayed_minutes={retry['delayed_minutes']}")
    if cfg.get("mode") == "lean":
        print(LEAN_HINT)
    if project.needs_backend_migration:
        print("迁移提示：旧项目继续使用 codex；Windows v2 也使用 codex，可用 baton init --migrate 迁移配置。"
              if os.name == "nt" else "迁移提示：旧项目继续使用 codex；v2 默认执行者将改为 Hermes，第 4 阶段提供 baton init --migrate。",
              file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    print(f"conductor.model：{conductor['model']}")
    print(f"conductor.council_for_major_decisions：{conductor['council_for_major_decisions']}")
    print(f"judge.council_for_major_decisions（v1 旧名，等同上一行）：{conductor['council_for_major_decisions']}")
    for key in ("model", "reasoning_effort", "final_reasoning_effort", "timeout_minutes"):
        print(f"monitor.{key}：{monitor.get(key)}")
    if project.judge_renamed_to_monitor:
        print("迁移提示：项目层的 judge 节已按 v2 早期草案改名为 monitor。")
    for key in ("backend", "hermes_profile", "provider", "toolsets", "sandbox_exec", "model", "reasoning_effort", "service_tier", "disable_fast_mode", "network_access", "sandbox"):
        print(f"executor.{key}：{ex.get(key)}")
    for key in ("immediate", "delayed", "delayed_minutes"):
        print(f"executor.capacity_retry.{key}：{retry[key]}")
    escalation = ex.get("escalation") or {}
    print("executor.escalation：" + json.dumps(escalation, ensure_ascii=False, sort_keys=True))
    print(f"项目目录：{project.root}")
    print(f"项目 id：{project.project_id}")
    print(f"项目外状态目录：{project.state_dir}")
    print(f"supervision.test_command：{cfg['supervision'].get('test_command', '')}")
    for key in ("interval_minutes", "max_unsupervised_minutes", "first_check_minutes", "max_check_minutes",
                "unhandled_stop_minutes", "rule_check_seconds", "check_summary_max_chars"):
        print(f"supervision.{key}：{cfg['supervision'].get(key)}")
    print("配置层：")
    for path, exists in project.config_paths():
        print(f"- {path}：{'存在' if exists else '缺失'}")


def cmd_quota(project, args):
    argv = (["--json"] if args.json else []) + (["--refresh"] if args.refresh else []) + ["--notify"]
    sys.exit(codex_quota.main(argv))


COUNCIL_BUILTIN = ["council-cc-glm", "council-codex-gpt", "council-cc-kimi"]  # council.sh's own fallback


def doctor_hermes(project, cfg, line):
    """Run Hermes-specific checks; returns whether any check failed."""
    failed = False

    def check(level, message):
        nonlocal failed
        line(level, message)
        failed = failed or level == "FAIL"

    hermes = hermes_executable(cfg)
    hermes_available = os.path.isfile(hermes) or shutil.which(hermes)
    check("OK" if hermes_available else "FAIL", f"hermes CLI：{hermes_available or '未安装'}")
    if hermes_available:
        version = subprocess.run([hermes, "--version"], env=_hermes_subprocess_env(cfg),
                                 capture_output=True, text=True, encoding="utf-8")
        version_text = version.stdout or version.stderr or ""
        version_line = next((item.strip() for item in version_text.splitlines() if item.strip()), "未知")
        check("OK" if version.returncode == 0 else "WARN",
              "Hermes 版本：" + version_line)
    profile = hermes_profile_dir(cfg)
    check("OK" if os.path.isdir(profile) else "FAIL", f"Hermes profile：{profile}")
    expected_soul, expected_config = hermes_soul_text(), hermes_config_text(cfg)
    soul_path, config_path = os.path.join(profile, "SOUL.md"), os.path.join(profile, "config.yaml")
    check("OK" if read_text(soul_path) == expected_soul else "FAIL", "SOUL.md 与模板一致")
    check("OK" if read_text(config_path) == expected_config else "FAIL", "config.yaml 与模板一致")
    env_path = os.path.join(profile, ".env")
    check("OK" if not hermes_env_has_assignments(env_path) else "FAIL", ".env 为空或不存在")
    entries = _profile_credential_entries(hermes_profile_auth_path(cfg))
    check("OK" if len(entries) == 1 else "FAIL",
          f"profile auth.json：openai-codex 凭据 {len(entries)} 条（必须恰好 1 条）")
    if len(entries) == 1:
        account = hermes_account_ids(cfg)
        profile_id, codex_id = account["profile"], account["codex"]
        if account["mismatch"]:
            check("FAIL", f"账号 id 哈希不一致：{_account_id_hash(profile_id) or '缺失'}（Codex {_account_id_hash(codex_id) or '缺失'}）")
        elif profile_id and codex_id:
            check("OK", f"账号 id 哈希：{_account_id_hash(profile_id)}（Codex {_account_id_hash(codex_id)}）")
        else:
            check("WARN", "无法从 profile/Codex auth.json 解析账号 id，未比较哈希")
    baseline_path = os.path.join(project.state_dir, "hermes-root-auth.sha256")
    baseline = read_text(baseline_path).strip() if os.path.exists(baseline_path) else ""
    current = _sha256_file(hermes_root_auth_path(cfg)) or "missing"
    if baseline:
        check("OK" if baseline == current else "WARN", "根目录 auth.json 哈希未变化" if baseline == current else "根目录 auth.json 哈希已变化")
    else:
        check("WARN", "没有 init 保存的根目录 auth.json 哈希")
    sandbox = shutil.which("sandbox-exec")
    check("OK" if sandbox else "FAIL", f"sandbox-exec：{sandbox or '未安装'}")
    return failed


def doctor_memory(project, cfg, line):
    """Check the real memory venv, Ollama model/dimensions, MCP and ledger."""
    if (cfg.get("memory") or {}).get("backend") == "local":
        line("WARN", "memory.backend=local: keyword/ledger memory; Ollama/mem0 optional and not required")
        return False
    python = memory_venv_python(cfg)
    if not python:
        line("WARN", "记忆工具未安装（venv 不存在），本轮执行者照常运行")
        return False
    failed = False

    def check(level, message):
        nonlocal failed
        line(level, message)
        failed = failed or level == "FAIL"

    env = os.environ.copy()
    env["PYTHONPATH"] = HERE + os.pathsep + env.get("PYTHONPATH", "")
    script = ("from baton_memory.ledger import MemoryLedger; "
              "from baton_memory.mem0_adapter import ensure_model, embed_text, MODEL_DIMS; "
              "ensure_model(); v=embed_text('Baton doctor'); print('DIMS='+str(len(v))); "
              "assert len(v)==MODEL_DIMS")
    probe = subprocess.run([python, "-c", script], cwd=project.root, env=env,
                           capture_output=True, text=True, timeout=30, encoding="utf-8")
    if probe.returncode != 0:
        check("FAIL", "Ollama/嵌入检查失败：" + (probe.stderr or probe.stdout).strip()[-500:])
    else:
        dims = next((line_text.split("=", 1)[1] for line_text in probe.stdout.splitlines()
                     if line_text.startswith("DIMS=")), "?")
        check("OK", "Ollama 在线，模型带 tag 且嵌入维度为 " + dims)
    collection_script = ("from baton_memory.ledger import MemoryLedger; "
                         "from baton_memory.mem0_adapter import build_mem0, COLLECTION, MODEL_DIMS; "
                         "m=build_mem0(MemoryLedger(%r)); "
                         "c=m.vector_store.client.get_collection(COLLECTION); "
                         "v=c.config.params.vectors; size=getattr(v, 'size', None) if not isinstance(v, dict) else next(iter(v.values())).size; "
                         "print('COLLECTION_DIMS='+str(size)); assert size==MODEL_DIMS" % project.root)
    collection = subprocess.run([python, "-c", collection_script], cwd=project.root, env=env,
                                capture_output=True, text=True, timeout=30, encoding="utf-8")
    if collection.returncode == 0:
        value = next((line_text.split("=", 1)[1] for line_text in collection.stdout.splitlines()
                      if line_text.startswith("COLLECTION_DIMS=")), "?")
        check("OK", "已有 collection 维度为 " + value)
    elif "does not exist" in (collection.stderr or "").lower() or "not found" in (collection.stderr or "").lower():
        check("WARN", "尚无已建 collection，首次写入时创建")
    else:
        check("FAIL", "collection 维度检查失败：" + (collection.stderr or collection.stdout).strip()[-500:])
    imports = subprocess.run([python, "-c", "import mem0, mcp, ollama"], env=env,
                             capture_output=True, text=True, timeout=30, encoding="utf-8")
    check("OK" if imports.returncode == 0 else "FAIL",
          "venv import mem0/mcp/ollama" if imports.returncode == 0 else "venv import 失败：" + imports.stderr.strip()[-500:])
    good, error = __import__("baton_memory.ledger", fromlist=["MemoryLedger"]).MemoryLedger(project).verify(repair=True)
    check("OK" if good else "FAIL", "记忆台账校验通过" if good else "记忆台账校验失败：" + error)
    if cfg["executor"].get("backend") == "hermes":
        try:
            ready, reason = memory_mcp_preflight(project, "doctor", 0, cfg)
            check("OK" if ready else "FAIL", "baton-memory tools/list 含且仅含两个只读工具" if ready else reason)
        except Exception as exc:
            check("FAIL", "baton-memory tools/list 失败：" + str(exc))
    return failed


def cmd_memory(project, args):
    """Memory subcommands; all third-party work runs in the memory venv."""
    from baton_memory.ledger import LedgerError, MemoryLedger
    if args.memory_action == "install":
        from baton_memory.install import install_memory
        result = install_memory(baton_home=memory_baton_home(project.config),
                                python=getattr(args, "python", None), scripts_dir=HERE)
        print("记忆环境已安装：%s" % result["venv"])
        return
    project.require_init()
    if args.memory_action == "seed":
        task = getattr(args, "task", None) or "seed"
        check_task(task)
        if not project.read_state(task, None):
            project.update_state(task, lambda s: s.update(task=task, status="idle", legs=[{
                "n": 1, "kind": "seed", "status": "report", "executor_status": "report",
                "started_at": now(), "events": "", "result": {"status": "report", "artifact": ""}}]))
        dossier = build_seed_dossier(project, task)
        if not dossier:
            die("lean 模式或 monitor.enabled=false 不生成 seed 卷宗")
        _start_monitor_process(project, task, dossier, curation=False)
        print("Seed 卷宗：%s；monitor 完成后运行 baton rule %s %s <裁决文件>" % (dossier, task, dossier))
        return
    ledger = MemoryLedger(project)
    if args.memory_action == "verify":
        good, error = ledger.verify(repair=True)
        if good:
            print("记忆台账校验通过")
        else:
            print("记忆台账校验失败：%s" % error)
            raise SystemExit(1)
    elif args.memory_action == "reanchor":
        task = getattr(args, "task", "") or ""
        _require_task_confirmation(task)
        try:
            head = ledger.reanchor(lambda _: True)
        except LedgerError as exc:
            die(str(exc))
        print("记忆台账链头已重设：%s" % head)
    elif args.memory_action == "reindex":
        from baton_memory.mem0_adapter import Mem0Index
        try:
            count = Mem0Index(ledger).reindex(all_records=bool(getattr(args, "all", False)))
        except Exception as exc:  # dimensions and other mem0 errors are intentionally visible
            die(str(exc))
        print("已补建 %d 条记忆索引" % count)


def council_dir():
    return os.path.join(skills_dir(), "council")


def council_check():
    """(level, message) about council's config.json and the councilor functions it names."""
    cfg_path = os.path.join(council_dir(), "config.json")
    cfg = read_json(cfg_path)
    note = ""
    if not isinstance(cfg, dict):
        cfg, note = {}, f"{cfg_path} 不是合法 JSON，council.sh 会退回内置顾问；"
    names = [n for n in (cfg.get("councilors") or []) if isinstance(n, str)]
    if not names:
        names, note = list(COUNCIL_BUILTIN), note or "config.json 没有列出顾问，council.sh 会用内置顾问；"
    if cfg.get("chief_backend"):
        names.append(cfg["chief_backend"])
    lib = os.environ.get("COUNCILOR_LIB") or os.path.expanduser("~/.local/bin/council-def.sh")
    if not os.path.exists(lib):
        return "WARN", f"council 后端定义 {lib} 不存在"
    script = 'source "$1" >/dev/null 2>&1; shift; for f in "$@"; do type "$f" >/dev/null 2>&1 || echo "$f"; done'
    missing = run(["bash", "-c", script, "_", lib, *names], check=False).split()
    if missing or note:
        return "WARN", f"council 后端（{lib}）：{note}" + (f"缺少 {'、'.join(missing)}" if missing else "")
    return "OK", f"council 后端（{lib}）：配置里的顾问都已定义"


def cmd_doctor(project, args):
    ok = True

    def line(level, msg):
        nonlocal ok
        ok = ok and level != "FAIL"
        print(f"[{level}] {msg}")
    try:
        cfg = project.config
    except ConfigError as e:
        line("FAIL", str(e))
        sys.exit(1)
    ex = cfg["executor"]
    for path in legacy_claude_instruction_warnings(project):
        line("WARN", f"旧版 Baton 指令出现在 {path}：请改用 conductor.model、monitor 监督简报和 baton rule，不能派子 agent 代替 monitor")
    codex = shutil.which("codex.exe" if os.name == "nt" else "codex")
    codex_level = "WARN" if ex.get("backend") == "hermes" and not codex else ("OK" if codex else "FAIL")
    line(codex_level, f"codex CLI：{codex or '未安装'}")
    if codex:
        line("OK", run([platform_support.executable("codex"), "--version"], check=False).strip())
    if ex.get("backend") == "hermes":
        if os.name == "nt":
            line("FAIL", WINDOWS_HERMES_ERROR)
        else:
            ok = not doctor_hermes(project, cfg, line) and ok
    ok = not doctor_memory(project, cfg, line) and ok
    baton_state = "存在" if os.path.exists(project.baton_config_path) else "缺失"
    line("OK" if baton_state == "存在" else "WARN",
         f"conductor 模型：{cfg['conductor']['model']}；monitor 模型：{cfg['monitor']['model']}；"
         f"baton.json：{baton_state}（{project.baton_config_path}）")
    cache = read_json(os.path.expanduser("~/.codex/models_cache.json"), {})
    models = cache.get("models", cache) if isinstance(cache, dict) else cache
    model = next((m for m in models or [] if isinstance(m, dict) and m.get("slug") == ex["model"]), None)
    if model:
        efforts = [x.get("effort") for x in model.get("supported_reasoning_levels") or []]
        line("OK" if ex["reasoning_effort"] in efforts else "FAIL",
             f"模型 {ex['model']}，思考深度 {ex['reasoning_effort']}（可选：{'/'.join(efforts)}）")
    else:
        line("WARN", f"models_cache 里没找到 {ex['model']}（可能只是缓存未刷新）")
    line("OK" if ex.get("service_tier") != "priority" and ex.get("disable_fast_mode") else "WARN",
         f"service_tier={ex.get('service_tier')}，disable_fast_mode={ex.get('disable_fast_mode')}（加速模式应关闭）")
    data = safe_quota(block=True)
    desc = codex_quota.describe(data)
    if codex_quota.is_low(data):
        print(desc.splitlines()[0])
        line("WARN", desc.splitlines()[-1])
    else:
        line("OK" if data and data.get("source") == "live" else "WARN", desc.splitlines()[-1])
    launcher = os.path.realpath(os.path.join(SKILL_DIR, "bin", "baton.cmd" if os.name == "nt" else "baton"))
    on_path = shutil.which("baton")
    if not on_path:
        line("WARN", f"PATH 上没有 baton；可以直接用 {launcher}")
    elif os.path.normcase(os.path.realpath(on_path)) != os.path.normcase(launcher):
        line("WARN", f"PATH 上的 baton（{on_path}）不是这个 skill 的，请改用 {launcher} 或处理同名冲突")
    else:
        line("OK", f"baton 命令：{on_path}")
    council = os.path.join(council_dir(), "scripts", "launch.sh")
    line("OK" if os.path.exists(council) else "WARN",
         "council skill：" + ("已安装" if os.path.exists(council) else f"未安装（{council_dir()}，./install.sh --council）"))
    if os.path.exists(council):
        line(*council_check())
    top = git_repo_top(project.root)
    line("OK", f"项目 {project.root}（id {project.project_id}）："
         + (f"在 git 仓库里，git 顶层 {top}" if top else "不是 git 仓库"))
    shadow_ok = os.path.isfile(os.path.join(project.snap_git_dir, "HEAD"))
    line("OK" if shadow_ok else "WARN",
         f"快照：影子仓库 {project.snap_git_dir}" + ("" if shadow_ok else "（尚未建立，baton init 会建立）"))
    line("OK" if os.path.isdir(project.dir) else "WARN", ".baton/：" + ("已初始化" if os.path.isdir(project.dir) else "未初始化（baton init）"))
    sl = statusline_report(project).splitlines()[0]
    good = "已安装 Baton 版本" in sl
    line("OK" if good else "WARN", sl + ("" if good else "；baton init 或 baton statusline install"))
    if good and is_git(project.root) and (settings_tracked(project) or not settings_ignored(project)):
        line("WARN", ".claude/settings.local.json 没有被 git 忽略，里面的本机路径可能被提交")
    if getattr(args, "monitor_probe", False):
        probe = run_monitor_probe(project, cfg, line)
        os.makedirs(project.state_dir, exist_ok=True)
        write_json(os.path.join(project.state_dir, "monitor-probe.json"), probe)
    else:
        print("提示：要检查 monitor 的隔离环境，请加 --monitor-probe")
    sys.exit(0 if ok else 1)


def _probe_normalize_line(value):
    value = re.sub(r"^[#>*\-\s]+", "", str(value or ""))
    return re.sub(r"\s+", " ", value).strip()


def _probe_candidate_lines(lines):
    candidates = []
    for value in lines:
        text = str(value or "").strip()
        if not text or text.startswith("<!--") or text.endswith("-->"):
            continue
        normalized = _probe_normalize_line(text)
        if not normalized or not re.search(r"[\w\u3400-\u9fff]", normalized):
            continue
        candidates.append(text)
    return candidates


def run_monitor_probe(project, cfg, line):
    """Run the opt-in Codex/Claude isolation probes used by dossier evidence."""
    result = {"at": now(), "codex_global_instructions": None, "codex_dossier_read": False, "claude_ok": False}
    probe_dir = os.path.join(project.state_dir, "monitor-probe")
    os.makedirs(probe_dir, exist_ok=True)
    # Not tempfile.mkdtemp: on Windows (Python 3.12.4+) it creates an owner-only ACL that the Codex
    # unelevated sandbox cannot read. Real dossiers live under state_dir, so the probe does too.
    root = os.path.join(probe_dir, "run-" + os.urandom(6).hex())
    os.makedirs(root)
    try:
        dossier = os.path.join(root, "dossier")
        os.makedirs(os.path.join(dossier, "code"), exist_ok=True)
        read_token = "BATON_DOSSIER_READ_" + os.urandom(16).hex()
        _dossier_write(os.path.join(dossier, "charter.md"), read_token + "\n")
        _seal_dossier(dossier)
        output = os.path.join(root, "codex.last.txt")
        monitor_cfg = dict(cfg)
        monitor_cfg["monitor"] = dict(cfg.get("monitor") or {}, model=(cfg.get("monitor") or {}).get("model", "gpt-5.6-sol"))
        monitor_model = monitor_cfg["monitor"]["model"]
        if monitor_model in CONDUCTOR_MODELS:
            result["codex_skipped"] = True
            line("OK", f"Codex monitor 探针：monitor.model={monitor_model} 是 Claude 别名，已跳过")
        else:
            if not str(monitor_model).lower().startswith("gpt-"):
                skill_cfg = read_json(os.path.join(SKILL_DIR, "config.json"), {}) or {}
                monitor_model = ((skill_cfg.get("monitor") or {}).get("model") or "gpt-5.6-sol")
                monitor_cfg["monitor"]["model"] = monitor_model
        codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
        agents_path = os.path.join(codex_home, "AGENTS.md")
        nonempty = [line_text.strip() for line_text in read_text(agents_path).splitlines() if line_text.strip()]
        first = nonempty[0] if nonempty else "（文件不存在）"
        candidates = _probe_candidate_lines(nonempty[1:])[:3]
        events_path = os.path.join(probe_dir, "codex.events.jsonl")
        last_path = os.path.join(probe_dir, "codex.last.txt")
        stderr_path = os.path.join(probe_dir, "codex.stderr.log")
        result.update(codex_events=events_path, codex_last=last_path, codex_stderr=stderr_path)
        for raw_path in (events_path, last_path, stderr_path):
            Path(raw_path).touch()
        codex_prompt = ("先用文件读取工具读取当前目录的 charter.md，原样输出其内容；内容只在文件里，不能猜测。\n"
                        "然后，如果你收到的指令中出现下面给出的第一行，请原样输出其后前三个候选行中的任意一行；"
                        "HTML 注释和只有标点的行不算候选。如果看不到这第一行，或没有候选行，第二部分只输出 NONE。\n"
                        f"第一行：{first}")
        command = [platform_support.executable("codex"), "exec", "--json", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                   "--skip-git-repo-check", "-s", "read-only", "-C", dossier,
                   "-m", monitor_model, *platform_support.codex_readonly_config(), "-o", output, "-"]
        try:
          if not result.get("codex_skipped"):
            p = subprocess.run(command, cwd=dossier, input=codex_prompt, text=True,
                               capture_output=True, timeout=60, env=os.environ.copy(), encoding="utf-8")
            with open(events_path, "w", encoding="utf-8") as stream:
                stream.write(p.stdout or "")
            shutil.copyfile(output, last_path) if os.path.isfile(output) else Path(last_path).write_text("", encoding="utf-8")
            Path(stderr_path).write_text(p.stderr or "", encoding="utf-8")
            result["codex_events"] = events_path
            result["codex_last"] = last_path
            result["codex_stderr"] = stderr_path
            events = []
            for event_line in (p.stdout or "").splitlines():
                try:
                    event = json.loads(event_line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            failures = [event.get("error") for event in events if event.get("type") == "turn.failed"]
            last_message = read_text(last_path).strip()
            if p.returncode != 0 or failures or not last_message:
                failure_text = ""
                if failures:
                    failure = failures[-1]
                    failure_text = failure.get("message") if isinstance(failure, dict) else str(failure)
                failure_text = failure_text or (p.stderr or "").strip() or f"退出码 {p.returncode}"
                result["codex_error"] = failure_text[:1000]
                line("FAIL", "Codex 探针没能运行：" + result["codex_error"])
            else:
                result["codex_dossier_read"] = read_token in last_message
                read_error = _monitor_read_error(None, (p.stdout or "") + "\n" + (p.stderr or ""))
                if not result["codex_dossier_read"] or read_error:
                    result["codex_dossier_read"] = False
                    result["codex_error"] = read_error or "未能读出卷宗 charter.md 中的内容"
                    line("FAIL", "Codex monitor 卷宗读取探针失败：" + result["codex_error"])
                else:
                    line("OK", "Codex monitor 卷宗读取探针：已读出 charter.md")
                normalized_output = _probe_normalize_line(last_message)
                if candidates and any(_probe_normalize_line(candidate) in normalized_output for candidate in candidates):
                    result["codex_global_instructions"] = True
                    line("WARN", "Codex monitor 探针：monitor 带着用户的 Codex 全局指令（~/.codex/AGENTS.md）")
                elif nonempty:
                    result["codex_global_instructions"] = False
                    line("OK", "Codex monitor 探针：未发现用户全局指令（未复述候选行）")
                else:
                    result["codex_global_instructions"] = False
                    line("OK", "Codex monitor 探针：~/.codex/AGENTS.md 不存在")
        except Exception as e:  # noqa: BLE001
            line("FAIL", "Codex monitor 探针失败：" + str(e))
            result["codex_error"] = str(e)
        secret = "BATON_CLAUDE_PROBE_SECRET"
        claude_stream_path = os.path.join(probe_dir, "claude.stream-json")
        result["claude_stream"] = claude_stream_path
        Path(claude_stream_path).touch()
        with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as stream:
            stream.write(secret + "\n")
        claude_cfg = dict(cfg)
        claude_cfg["monitor"] = dict(cfg.get("monitor") or {}, model="haiku")
        command = _monitor_claude_command(project, "monitor-probe", claude_cfg, stream=True)
        # The helper's dossier path is project based; replace it with the isolated probe dir.
        command[command.index("--add-dir") + 1] = os.path.join(dossier, "code")
        try:
            claude_prompt = "如果你在收到的指令中看到任何以 BATON_ 开头的口令，请原样输出你看到的口令；没有就只输出 NONE。"
            p = subprocess.run(command, cwd=dossier, input=claude_prompt, text=True,
                               capture_output=True, timeout=60, env=dict(os.environ,
                               CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
                               ENABLE_CLAUDEAI_MCP_SERVERS="false"), encoding="utf-8")
            Path(claude_stream_path).write_text(p.stdout or "", encoding="utf-8")
            if secret in (p.stdout or ""):
                line("FAIL", "Claude monitor 探针：上级 CLAUDE.md 口令泄露")
                result["claude_error"] = "上级 CLAUDE.md 口令泄露"
            else:
                init = None
                for line_text in (p.stdout or "").splitlines():
                    try:
                        item = json.loads(line_text)
                    except ValueError:
                        continue
                    if item.get("type") == "system" and item.get("subtype") == "init":
                        init = item
                        break
                tools = (init or {}).get("tools") or []
                permission = (init or {}).get("permissionMode") or (init or {}).get("permission_mode")
                mcp = (init or {}).get("mcp_servers") or (init or {}).get("mcpServers") or []
                result["claude_ok"] = set(tools) == {"Read", "Grep", "Glob", "StructuredOutput"} and permission == "dontAsk" and not mcp
                if result["claude_ok"]:
                    line("OK", "Claude monitor 探针：工具、MCP、权限和上级口令隔离符合预期")
                else:
                    problems = []
                    if set(tools) != {"Read", "Grep", "Glob", "StructuredOutput"}:
                        problems.append("工具集合不符")
                    if permission != "dontAsk":
                        problems.append("权限模式不是 dontAsk")
                    if mcp:
                        problems.append("存在 MCP server")
                    line("WARN", "Claude monitor 探针：" + "、".join(problems))
        except Exception as e:  # noqa: BLE001
            line("FAIL", "Claude monitor 探针失败：" + str(e))
            result["claude_error"] = str(e)
    finally:
        _unseal_dossier(root)
        shutil.rmtree(root)
    return result


def main():
    ap = argparse.ArgumentParser(prog="baton", description="Claude 指挥、Codex 执行的工作流工具")
    ap.add_argument("--root", help="项目目录（默认：从当前目录往上第一个含 baton.json 的目录，不越过 git 根）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="在项目里创建 .baton/，并配置带 Codex 额度的 statusline")
    s.add_argument("--no-statusline", action="store_true", help="不改项目的 statusline")
    s.add_argument("--accept-boundary", action="store_true", help="确认项目目录可以是 git 顶层下的子目录")
    s.add_argument("--relink", action="store_true", help="项目目录换过位置或名字时，把原来的项目外状态接过来")
    s.add_argument("--fresh", action="store_true", help="复制项目或原状态不存在时，开始新的项目外状态")
    s.add_argument("--upgrade-profile", action="store_true", help="明确覆盖已有 Hermes profile 的 SOUL.md/config.yaml，并打印前后差异")
    s.add_argument("--migrate", action="store_true", help="把项目根 baton.json 从 v1 迁移到 v2，并保留 .v1.bak")
    s = sub.add_parser("statusline", help="项目 statusline 的 Codex 额度行")
    s.add_argument("action", nargs="?", default="status", choices=["install", "uninstall", "status"])
    s.add_argument("--force", action="store_true", help="settings.local.json 被 git 跟踪或指向项目外时也安装")
    s = sub.add_parser("new", help="新建任务并生成简报模板")
    s.add_argument("task")
    s.add_argument("--brief", help="用已有文件作为简报")
    s = sub.add_parser("start", help="启动首轮（codex exec）")
    s.add_argument("task")
    for name, helptext in (("resume", "带指挥消息续跑（codex exec resume）"), ("steer", "停止当前轮并带纠偏消息续跑")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("task")
        s.add_argument("message_file", nargs="?")
        s.add_argument("--message")
        if name == "resume":
            s.add_argument("--kind", default="回复", help="消息类型：决策/审阅/纠偏/回复")
            s.add_argument("--verdict", help="用指定卷宗最新裁决原文续跑")
            s.add_argument("--decision", help="用指定 D 号最新决定原文续跑")
            s.add_argument("--user-approved-continue", action="store_true")
            s.add_argument("--escalate", action="store_true", help="本轮强制使用 executor.escalation 模型")
    s = sub.add_parser("wait", help="阻塞到本轮结束、监管时间到或额度告警（请在后台运行）")
    s.add_argument("task")
    s.add_argument("--tick", type=float, help="监管间隔分钟数（默认取配置）")
    s = sub.add_parser("monitor", help="运行或重跑指定卷宗的监督 monitor")
    s.add_argument("task")
    s.add_argument("dossier", nargs="?")
    s.add_argument("--rerun", action="store_true")
    s.add_argument("--curate", action="store_true", help="生成并运行记忆整理卷宗")
    s = sub.add_parser("rule", help="逐条回应监督简报并登记裁决")
    s.add_argument("task")
    s.add_argument("dossier")
    s.add_argument("verdict_file")
    s.add_argument("--rerule", action="store_true")
    s.add_argument("--memory", choices=["as-advised", "none"], default="as-advised",
                   help="记忆建议的默认核准行为（默认 as-advised）")
    s.add_argument("--curation", action="store_true", help="确认整理卷宗而非普通监督卷宗")
    s = sub.add_parser("decide", help="冻结决策请求并登记指挥决定")
    s.add_argument("task")
    s.add_argument("decision_id")
    s.add_argument("decision_file")
    s.add_argument("--rerule", action="store_true")
    s = sub.add_parser("status", help="监管快照")
    s.add_argument("task")
    s = sub.add_parser("close", help="验收通过后关闭任务")
    s.add_argument("task")
    s.add_argument("--abandon", metavar="REASON",
                   help="没有 ACCEPT 终审也关闭（如以 REPORT 结束的任务），写入账本并清理卷宗代码")
    s = sub.add_parser("gc", help="清理已 closed 任务的卷宗代码，保留记录和账本")
    s.add_argument("--dry-run", action="store_true", help="只列出将清理的卷宗、文件数和字节数")
    s = sub.add_parser("supervised", help="记录一次监管结论并重置计时")
    s.add_argument("task")
    s.add_argument("--verdict", choices=["on_track", "drifting", "breaking"])
    s.add_argument("--note")
    s.add_argument("--dismiss", action="append", default=[])
    s.add_argument("--reason")
    s = sub.add_parser("stop", help="中断当前轮")
    s.add_argument("task")
    s.add_argument("--reason")
    s = sub.add_parser("checkpoint", help="手动建回滚点")
    s.add_argument("task")
    s.add_argument("label", nargs="?")
    s = sub.add_parser("refs", help="列出回滚点")
    s.add_argument("task")
    s = sub.add_parser("diff", help="相对回滚点的改动")
    s.add_argument("task")
    s.add_argument("--since", default="leg", help="base / leg / tick / report / baton refs 列出的回滚点")
    s.add_argument("--stat", action="store_true")
    s = sub.add_parser("rollback", help="把工作区文件恢复到某个回滚点（先自动保存当前状态）")
    s.add_argument("task")
    s.add_argument("ref")
    s.add_argument("--yes", action="store_true")
    s = sub.add_parser("restore-frozen", help="从项目外基线恢复锁定级文件")
    s.add_argument("task")
    s.add_argument("paths", nargs="*", help="只恢复指定的违规路径；缺省处理全部")
    s = sub.add_parser("amend", help="登记任务修正案、配置快照变更或重新冻结文件")
    s.add_argument("task")
    s.add_argument("amendment_file", nargs="?", help="修正案 Markdown 文件")
    s.add_argument("--config", action="append", default=[], metavar="键=值")
    s.add_argument("--adopt", action="append", default=[], metavar="路径")
    s.add_argument("--user-approved", action="store_true")
    sub.add_parser("list", help="列出任务")
    s = sub.add_parser("config", help="显示生效配置")
    s.add_argument("--json", action="store_true", help="输出完整合并后的 JSON 配置")
    s = sub.add_parser("quota", help="Codex 额度")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true")
    s = sub.add_parser("doctor", help="环境自检")
    s.add_argument("--monitor-probe", action="store_true", help="运行 monitor 隔离探针")
    s = sub.add_parser("memory", help="项目记忆台账、索引与部署")
    memory_sub = s.add_subparsers(dest="memory_action", required=True)
    m = memory_sub.add_parser("install", help="创建记忆 venv、安装 mem0/mcp/ollama 并派生嵌入模型")
    m.add_argument("--python", help="指定 uv CPython 3.11 路径")
    memory_sub.add_parser("verify", help="校验记忆台账哈希链")
    m = memory_sub.add_parser("reanchor", help="在终端确认后重设记忆台账链头")
    m.add_argument("task", nargs="?", default="")
    m = memory_sub.add_parser("reindex", help="补建 index_pending 记忆索引")
    m.add_argument("--all", action="store_true", help="新建 collection 并全量重灌")
    m = memory_sub.add_parser("seed", help="从项目现有记录生成初版记忆 seed 卷宗")
    m.add_argument("task", nargs="?", default="seed")
    s = sub.add_parser("_run")
    s.add_argument("task")
    s.add_argument("n")
    s = sub.add_parser("_monitor")
    s.add_argument("--marker", default="")
    s.add_argument("task")
    s.add_argument("dossier")
    s = sub.add_parser("_monitor_check")
    s.add_argument("task")
    s.add_argument("dossier")
    args = ap.parse_args()
    home = os.environ.get("BATON_HOME")
    if home:
        expanded_home = os.path.expanduser(home)
        if "${" in home:
            os.environ["BATON_HOME"] = os.path.abspath(os.path.expanduser("~/.baton"))
        else:
            os.environ["BATON_HOME"] = os.path.abspath(expanded_home)
    root = canonical_dir(args.root) if args.root else find_project_dir(os.getcwd(), legacy=args.cmd != "init")
    project = Project(root)
    if args.cmd not in ("init", "quota", "doctor", "config", "statusline", "memory"):
        project.require_init()
    if args.cmd not in ("init", "quota", "memory") and is_initialized(project.root):
        if not project.recorded_id():
            project.record_id()
        project.check_identity(warn_only=args.cmd in ("doctor", "config"))
    handler = {
        "init": cmd_init, "statusline": cmd_statusline, "new": cmd_new, "start": cmd_start,
        "resume": cmd_resume, "steer": cmd_steer, "wait": cmd_wait, "monitor": cmd_monitor, "status": cmd_status,
        "close": cmd_close, "gc": cmd_gc,
        "supervised": cmd_supervised, "stop": cmd_stop, "checkpoint": cmd_checkpoint, "refs": cmd_refs,
        "diff": cmd_diff, "rollback": cmd_rollback, "list": cmd_list, "config": cmd_config, "quota": cmd_quota,
        "doctor": cmd_doctor, "memory": cmd_memory, "restore-frozen": cmd_restore_frozen, "amend": cmd_amend, "_run": cmd_run,
        "_monitor": cmd_monitor_run, "_monitor_check": cmd_monitor_check_run, "rule": cmd_rule, "decide": cmd_decide,
    }[args.cmd]
    if args.cmd == "steer":
        args.kind = "纠偏"
    try:
        handler(project, args)
    except ConfigError as e:
        die(str(e))


if __name__ == "__main__":
    main()
