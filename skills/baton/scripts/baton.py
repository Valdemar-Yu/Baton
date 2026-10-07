#!/usr/bin/env python3
"""Baton: Claude (conductor / judge) drives Codex CLI (executor).

Every Codex run is a "leg": `codex exec` (first leg) or `codex exec resume <thread>` (later legs).
A leg ends when the executor finishes, asks for a decision, hands in a major-change report,
gets blocked, or is stopped. Legs run detached, so they survive the Claude session; Claude
blocks on `baton wait` (in the background) and is woken by leg ends and 30-minute ticks.

State lives in <project>/.baton/ (see README). Python 3.9+, stdlib only.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

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

EXCLUDE = [":(exclude).baton", ":(exclude).council"]
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
BATON_GITIGNORE = ["runs/", "*.lock", "statusline-saved.json", "tasks/*/state.json", "rollback-backup/"]
LEG_END_EVENTS = {
    "done": "DONE", "decision": "DECISION", "report": "REPORT", "blocked": "BLOCKED",
    "failed": "FAILED", "stopped": "STOPPED", "paused_unsupervised": "PAUSED_UNSUPERVISED",
}
NEXT_STEP = {
    "DONE": "按 references/review.md 做最终验收（diff + 测试），再向用户汇报；不要自行提交。",
    "DECISION": "读决策请求文件，按 references/decision.md 决策（按 judge.council_for_major_decisions 决定是否用 council），然后 baton resume。",
    "REPORT": "按 references/review.md 审阅汇报 HTML，写 .baton/reviews/ 审阅意见，然后 baton resume。",
    "BLOCKED": "看 summary 里缺什么；能解决就解决后 baton resume，涉及权限/网络/费用先问用户。",
    "FAILED": "看错误和 stderr 末尾判断原因（额度、网络、参数）；修正后 baton resume 重试本轮。",
    "STOPPED": "已按指令停止。写纠偏/修复指令后 baton resume。",
    "PAUSED_UNSUPERVISED": "超过监管时限没有 Opus 检查，执行者已被暂停。先做一次监管检查，再 baton resume。",
    "TICK": "按 references/supervision.md 做一次监管检查，结束时运行 baton supervised 记录结论，然后重新后台 baton wait。",
    "QUOTA_LOW": "立刻在回复里提醒用户 Codex 额度不足，然后重新后台 baton wait。",
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
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def write_json_keep(path, data):
    """Atomic write that keeps the file's mode and writes through a symlink to its target."""
    real = os.path.realpath(path)
    try:
        mode = stat.S_IMODE(os.stat(real).st_mode)
    except OSError:
        mode = None
    tmp = f"{real}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # never readable by others mid-write
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, mode if mode is not None else 0o644)
    os.replace(tmp, real)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
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
    try:
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def pid_alive(pid, marker=None):
    """Process exists (and, with `marker`, its command line contains it: guards PID reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return marker is None
    return marker is None or marker in proc_cmdline(pid)


def die(msg, code=1):
    sys.stdout.flush()
    print(f"baton: {msg}", file=sys.stderr)
    sys.exit(code)


class ConfigError(Exception):
    """A project configuration error that should be shown to the caller."""


JUDGE_MODELS = {"opus", "sonnet", "haiku", "fable"}


def run(cmd, cwd=None, env=None, check=True, input_text=None):
    p = subprocess.run(cmd, cwd=cwd, env=env, input=input_text, capture_output=True, text=True)
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
    p = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                       capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


def git(root, *args, env=None, check=True, input_text=None):
    return run(["git", "-C", root, *args], env=env, check=check, input_text=input_text)


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
            shutil.copyfile(real_index, tmp_index)  # keeps the stat cache: fast add
        else:
            os.remove(tmp_index)
        env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
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
            p = subprocess.run(["git", "-C", top, "add", "-A", "--ignore-errors"], env=env,
                               capture_output=True, text=True)
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


def store_ref(root, task, label, commit):
    ref, k = f"refs/baton/{task}/{label}", 1
    while git(root, "rev-parse", "--verify", "-q", ref, check=False).strip():
        k += 1  # never overwrite an existing checkpoint
        ref = f"refs/baton/{task}/{label}-{k}"
    git(root, "update-ref", ref, commit)
    return ref


def checkpoint(root, task, label):
    if not is_git(root):
        return None
    return store_ref(root, task, label, snapshot(root, f"baton checkpoint {task}/{label} {stamp()}"))


def try_checkpoint(root, task, label):
    """checkpoint() that reports instead of raising: (ref or None, error or None)."""
    try:
        return checkpoint(root, task, label), None
    except Exception as e:  # noqa: BLE001 — a failed snapshot must not take the caller down
        return None, str(e)


def shortstat(root, a, b):
    """(files, insertions, deletions) between two commits, excluding Baton's own dirs."""
    out = git(root, "diff", "--shortstat", a, b, "--", ".", *EXCLUDE, check=False)

    def num(pattern):
        m = re.search(pattern, out)
        return int(m.group(1)) if m else 0
    return num(r"(\d+) files? changed"), num(r"(\d+) insertions?"), num(r"(\d+) deletions?")


def diff_tree_entries(root, a, b):
    """[(src_mode, dst_mode, status, top-relative path)] between two commits, under root.

    Plumbing (diff-tree) so user diff.* settings such as diff.relative or diff.renames
    cannot change the paths we act on.
    """
    raw = git(root, "diff-tree", "-r", "--raw", "-z", "--no-renames", a, b, "--", ".", *EXCLUDE)
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


# ---------------------------------------------------------------- project / state

def find_root(start):
    p = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else os.path.abspath(start)


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.dir = os.path.join(self.root, ".baton")

    @property
    def config(self):
        base = read_json(os.path.join(SKILL_DIR, "config.json"), {})
        project_cfg = read_json(os.path.join(self.dir, "config.json"), {})
        if not isinstance(project_cfg, dict):
            project_cfg = {}
        baton_cfg = self.read_baton_config()
        # quota belongs to the account-level skill configuration.  Keep the
        # project layers from changing it even though all other keys deep-merge.
        project_cfg = {k: v for k, v in project_cfg.items() if k != "quota"}
        baton_cfg = {k: v for k, v in baton_cfg.items() if k != "quota"}
        cfg = deep_merge(deep_merge(base, project_cfg), baton_cfg)
        judge = cfg.get("judge")
        if not isinstance(judge, dict):
            raise ConfigError("配置中的 judge 必须是 JSON object")
        judge_model = judge.get("model")
        if judge_model not in JUDGE_MODELS:
            raise ConfigError(
                f"judge.model 的值 {judge_model!r} 无效，只能是 opus、sonnet、haiku 或 fable"
            )
        return cfg

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

    def runs_dir(self, task):
        return self.p("runs", task)

    def leg_path(self, task, n, suffix):
        return os.path.join(self.runs_dir(task), f"leg-{n:03d}.{suffix}")

    def state_path(self, task):
        return os.path.join(self.task_dir(task), "state.json")

    def require_init(self):
        if not os.path.isdir(self.dir):
            die(f"{self.root} 还没有初始化，先运行 baton init")

    def load_state(self, task):
        st = read_json(self.state_path(task))
        if st is None:
            die(f"任务 {task} 不存在（{self.state_path(task)}）")
        return st

    def update_state(self, task, fn):
        """Read-modify-write the task state under a file lock."""
        os.makedirs(self.task_dir(task), exist_ok=True)
        with open(os.path.join(self.task_dir(task), "state.lock"), "w") as lock:
            if fcntl:
                fcntl.flock(lock, fcntl.LOCK_EX)
            st = read_json(self.state_path(task), {})
            fn(st)
            write_json(self.state_path(task), st)
            return st

    def tasks(self):
        d = self.p("tasks")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []


def current_leg(st):
    return st["legs"][-1] if st.get("legs") else None


def check_task(task):
    if not TASK_RE.fullmatch(task or ""):
        die("任务名只能用字母、数字、下划线和连字符，以字母或数字开头，不超过 64 个字符")


def runner_marker(task, n):
    return f"_run {task} {n}"


def codex_alive(leg):
    """The leg's own codex process is running (identified by its unique -o path, not just 'codex')."""
    return bool(leg) and not leg.get("codex_exited") and pid_alive(leg.get("codex_pid"), leg.get("last") or "\0")


def kill_codex(leg):
    """Stop the leg's codex process group (codex is started as a session leader). True if it was alive."""
    if not codex_alive(leg):
        return False
    pid = leg["codex_pid"]
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
    leg = current_leg(st)
    if not leg or leg.get("status") != "running":
        return st
    if not leg.get("runner_pid") and now() - leg["started_at"] < 30:
        return st  # launch_leg has not recorded the runner pid yet
    if pid_alive(leg.get("runner_pid"), runner_marker(task, leg["n"])):
        return st
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


def fmt_usage(u):
    def k(n):
        return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k"
    return f"输入 {k(u['input_tokens'])}（缓存 {k(u['cached_input_tokens'])}），输出 {k(u['output_tokens'])}"


def clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …"


# ---------------------------------------------------------------- prompts

def fill(template, mapping):
    for k, v in mapping.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def protocol_text(project, task):
    cfg = project.config
    return fill(read_text(os.path.join(REFERENCES, "executor-protocol.md")), {
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


def codex_command(project, kind, thread_id, last_path):
    ex = project.config["executor"]
    cmd = ["codex", "exec"] + (["resume"] if kind == "resume" else [])
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
    return subprocess.run(["git", "-C", top, "ls-files", "--error-unmatch", "--",
                           ":(icase)" + settings_rel_top(project)], capture_output=True).returncode == 0


def settings_ignored(project):
    top = git_top(project.root)
    return subprocess.run(["git", "-C", top, "check-ignore", "-q", "--", settings_rel_top(project)]).returncode == 0


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
        with open(path, "a") as f:
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
        with open(gi, "w") as f:
            f.write("# Baton runtime and machine-local files\n" + "\n".join(BATON_GITIGNORE) + "\n")
        return
    missing = [line for line in BATON_GITIGNORE if line not in have]
    if missing:
        with open(gi, "a") as f:
            f.write("\n" + "\n".join(missing) + "\n")


def cmd_init(project, args):
    # Validate an existing baton.json before changing the project state.  When
    # it is absent, the skill defaults and any existing .baton override form
    # the values used for the generated file below.
    project.config
    for d in ("tasks", "reports", "reviews", "decisions", "runs"):
        os.makedirs(project.p(d), exist_ok=True)
    write_baton_gitignore(project)
    if not os.path.exists(project.p("config.json")):
        write_json(project.p("config.json"), {
            "_comment": "Project overrides for Baton. Put model and test settings in <project>/baton.json; quota.* is ignored in project files.",
        })
    if not os.path.exists(project.baton_config_path):
        cfg = project.config
        write_json(project.baton_config_path, {
            "judge": {
                "model": cfg["judge"]["model"],
                "council_for_major_decisions": cfg["judge"].get("council_for_major_decisions", True),
            },
            "executor": {
                "model": cfg["executor"]["model"],
                "reasoning_effort": cfg["executor"]["reasoning_effort"],
                "service_tier": cfg["executor"]["service_tier"],
                "disable_fast_mode": cfg["executor"].get("disable_fast_mode", True),
                "network_access": cfg["executor"].get("network_access", False),
            },
            "supervision": {
                "test_command": cfg["supervision"].get("test_command", ""),
            },
        })
    if not os.path.exists(project.p("log.md")):
        with open(project.p("log.md"), "w") as f:
            f.write("# Baton 修改日志\n\n执行者每完成一处修改追加一条；major 条目指向 reports/ 下的汇报。\n")
    if not os.path.exists(project.p("supervision.md")):
        with open(project.p("supervision.md"), "w") as f:
            f.write("# Baton 监管记录\n\nOpus 每 30 分钟一次的检查结论。\n")
    print(f"已初始化 {project.dir}")
    if not getattr(args, "no_statusline", False):
        print(statusline_install(project))
    if not is_git(project.root):
        print("注意：不是 git 仓库，回滚点（checkpoint）不可用。建议先 git init；之后再运行一次 baton init。")


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
        with open(brief, "w") as f:
            f.write(text)
    print(brief)


def running_task(project, exclude=None):
    """Another task of this project with a running leg or a live codex, if any."""
    for task in project.tasks():
        if task == exclude:
            continue
        st = read_json(project.state_path(task), {}) or {}
        if not st.get("legs"):
            continue
        st = refresh_runner_liveness(project, task, st)
        leg = current_leg(st)
        if leg and (leg.get("status") == "running" or codex_alive(leg)):
            return task, leg["n"]
    return None


def launch_leg(project, task, kind, prompt):
    os.makedirs(project.dir, exist_ok=True)
    with open(project.p("launch.lock"), "w") as launch_lock:
        if fcntl:
            fcntl.flock(launch_lock, fcntl.LOCK_EX)  # one launch at a time per project
        st = read_json(project.state_path(task), {}) or {}
        st = refresh_runner_liveness(project, task, st) if st.get("legs") else st
        leg = current_leg(st) if st else None
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
        prompt_path = project.leg_path(task, n, "prompt.md")
        with open(prompt_path, "w") as f:
            f.write(prompt)

        print(codex_quota.describe(safe_quota(block=True, notify=True)))

        start_ref, err = try_checkpoint(project.root, task, f"leg-{n:03d}-start")
        if err:
            print(f"注意：本轮起点回滚点没建成：{err}")
        last_path = project.leg_path(task, n, "last.json")
        cmd = codex_command(project, kind, st.get("thread_id"), last_path)
        t0 = now()
        log_size = file_size(project.p("log.md"))

        def add_leg(s):
            s.setdefault("task", task)
            s.setdefault("created_at", t0)
            s.setdefault("legs", [])
            s["legs"].append({
                "n": n, "kind": kind, "status": "running", "started_at": t0,
                "start_ref": start_ref, "cmd": cmd, "prompt": prompt_path,
                "events": project.leg_path(task, n, "events.jsonl"), "last": last_path,
                "stderr": project.leg_path(task, n, "stderr.log"), "log_size_at_start": log_size,
            })
            s["status"] = "running"
            # resuming the executor is itself a supervision point: Opus has just looked at everything
            s["last_supervision_at"] = t0
            s["supervision"] = {"leg": n, "events_offset": 0, "log_offset": log_size, "ref": start_ref, "since": t0}
            s.pop("pending_supervision", None)
        project.update_state(task, add_leg)

        log = open(project.leg_path(task, n, "runner.log"), "w")
        runner = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--root", project.root,
                                   "_run", task, str(n)],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  start_new_session=True, cwd=project.root)

        def set_pid(s):
            current_leg(s)["runner_pid"] = runner.pid
        project.update_state(task, set_pid)
    cfg = project.config
    ex = cfg["executor"]
    print(f"BATON::STARTED task={task} leg={n} kind={kind} model={ex['model']} "
          f"effort={ex['reasoning_effort']} service_tier={ex['service_tier']} "
          f"judge={cfg['judge']['model']} runner_pid={runner.pid}")
    print(f"回滚点：{start_ref or '无'}")
    print(f"下一步：用后台方式运行 `baton wait {task}`，leg 结束或 "
          f"{project.config['supervision']['interval_minutes']} 分钟监管时间到会唤醒你。")


def cmd_start(project, args):
    project.require_init()
    check_task(args.task)
    brief_path = os.path.join(project.task_dir(args.task), "brief.md")
    brief = read_text(brief_path)
    if not brief.strip():
        die(f"缺少任务简报 {brief_path}；先 baton new {args.task} 并填写")
    if "（待填）" in brief or "{{TASK}}" in brief:
        die(f"简报里还有未填写的模板占位（「（待填）」或 {{{{TASK}}}}）：{brief_path}")
    st = read_json(project.state_path(args.task), {}) or {}
    if st.get("thread_id"):
        die(f"任务 {args.task} 已有 Codex 会话 {st['thread_id']}；继续请用 baton resume")
    if is_git(project.root):
        r = subprocess.run(["git", "check-ref-format", f"refs/baton/{args.task}/base"])
        if r.returncode != 0:
            die(f"任务名 {args.task} 不能用作 git ref，请换一个")
        write_baton_gitignore(project)
        if baton_statusline_installed(project):
            warn = ensure_settings_ignored(project)
            if warn:
                print(warn)
    if not st.get("base_ref"):
        ref, err = try_checkpoint(project.root, args.task, "base")
        if err:
            die(f"任务起点回滚点没建成：{err}")
        project.update_state(args.task, lambda s: s.update(base_ref=ref, task=args.task))
    prompt = (protocol_text(project, args.task)
              + f"\n\n---\n\n# 任务简报（{args.task}）\n\n" + brief)
    launch_leg(project, args.task, "start", prompt)


def message_from(args):
    """The conductor's message (file or --message), validated before anything else happens."""
    if args.message_file:
        if not os.path.isfile(args.message_file):
            die(f"找不到消息文件 {args.message_file}")
        message = read_text(args.message_file)
        if not message.strip():
            die(f"消息文件 {args.message_file} 是空的")
        return message
    if not (args.message or "").strip():
        die("需要指挥消息：给出消息文件，或用 --message TEXT")
    return args.message


def cmd_resume(project, args, message=None):
    project.require_init()
    message = message if message is not None else message_from(args)
    st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
    if not st.get("thread_id"):
        leg = current_leg(st)
        tid = thread_id_from(leg["events"]) if leg else None
        if not tid:
            die("找不到 Codex 会话 id，无法续跑；可能首轮没有成功启动，改用 baton start")
        project.update_state(args.task, lambda s: s.update(thread_id=tid))
    cfg = project.config
    prompt = (f"[Baton 指挥 · {args.kind}]\n\n{message.strip()}\n\n"
              "（继续遵守 Baton 执行协议：小修改记 log，大修改写汇报后停下，"
              "需要决策时写决策请求后停下；本轮结束按 JSON schema 输出。"
              f"当前 major 阈值：{cfg['change_size']['major_files']} 个文件或 {cfg['change_size']['major_lines']} 行；"
              f"验证命令：{cfg['supervision'].get('test_command') or '按简报'}。）\n")
    launch_leg(project, args.task, "resume", prompt)


def _interrupt(proc):
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
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    raise _Terminated(f"signal {signum}")


def cmd_run(project, args):
    """Internal: the detached leg runner. Always records a result, even when it is killed."""
    task, n = args.task, int(args.n)
    signal.signal(signal.SIGTERM, _raise_terminated)
    signal.signal(signal.SIGHUP, _raise_terminated)
    st = project.load_state(task)
    leg = st["legs"][n - 1]
    stop_flag = project.leg_path(task, n, "stop")
    proc, reason, error = None, None, None
    try:
        with open(leg["prompt"], "rb") as fin, open(leg["events"], "wb") as fout, \
                open(leg["stderr"], "wb") as ferr:
            proc = subprocess.Popen(leg["cmd"], cwd=project.root, stdin=fin, stdout=fout,
                                    stderr=ferr, start_new_session=True)

        def set_codex(s):
            current_leg(s)["codex_pid"] = proc.pid
        project.update_state(task, set_codex)

        limit = float(project.config["supervision"].get("max_unsupervised_minutes") or 0)
        while proc.poll() is None:
            time.sleep(2)
            if proc.poll() is not None:
                break  # finished on its own during the sleep: its result wins over a late stop
            if not st.get("thread_id"):
                tid = thread_id_from(leg["events"])
                if tid:
                    st = project.update_state(task, lambda s: s.update(thread_id=tid))
            if os.path.exists(stop_flag):
                reason = "stopped"
                _interrupt(proc)
                break
            last_sup = (read_json(project.state_path(task), {}) or {}).get("last_supervision_at", leg["started_at"])
            if limit and now() - last_sup > limit * 60:
                reason = "paused_unsupervised"
                _interrupt(proc)
                break
    except BaseException as e:  # noqa: BLE001 — killed or crashed: stop codex, still record the leg
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        error = f"runner 中断（{type(e).__name__}: {e}）"
        if proc is not None and proc.poll() is None:
            _interrupt(proc)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    rc = proc.wait() if proc is not None else -1

    result = read_json(leg["last"])
    if result is not None and not isinstance(result, dict):
        result = {"status": "unknown", "summary": json.dumps(result, ensure_ascii=False)[:4000],
                  "artifact": "", "change_size": "unknown"}
    if result is None and os.path.exists(leg["last"]):
        result = {"status": "unknown", "summary": read_text(leg["last"])[:4000],
                  "artifact": "", "change_size": "unknown"}
    finished_ok = rc == 0 and isinstance(result, dict) and result.get("status") in ("done", "decision", "report", "blocked")
    if finished_ok:
        status = result["status"]  # the executor's own result beats a stop that arrived too late
    elif error:
        status = "failed"
    elif reason:
        status = reason
    else:
        status = "failed"

    end_ref, cp_err = try_checkpoint(project.root, task, f"leg-{n:03d}-end")
    diff = None
    if end_ref and leg.get("start_ref"):
        try:
            diff = shortstat(project.root, leg["start_ref"], end_ref)
        except Exception:  # noqa: BLE001
            diff = None
    events, _ = read_events(leg["events"])
    dg = digest(events)
    log_size = file_size(project.p("log.md"))
    stderr_tail = read_text(leg["stderr"])[-1500:]
    notes = [x for x in (error if not finished_ok else None,
                         "收到停止指令时执行者已自己结束，按它的结果记录" if finished_ok and (reason or error) else None,
                         f"本轮终点回滚点没建成：{cp_err}" if cp_err else None) if x]

    def finish(s):
        lg = s["legs"][n - 1]
        lg.update(status=status, exit_code=rc, ended_at=now(), result=result, end_ref=end_ref,
                  diff=diff, usage=dg["usage"], log_grew=log_size > lg.get("log_size_at_start", 0),
                  stderr_tail=stderr_tail if status == "failed" else "", codex_exited=True)
        if notes:
            lg["error"] = "；".join(notes)
        if not s.get("thread_id"):
            s["thread_id"] = thread_id_from(lg["events"])
        s["status"] = "done" if status == "done" else "idle"
    project.update_state(task, finish)
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


def leg_event_text(project, task, leg):
    ev = LEG_END_EVENTS.get(leg["status"], str(leg["status"]).upper())
    lines = [f"BATON::EVENT {ev} task={task} leg={leg['n']}"]
    res = leg.get("result") if isinstance(leg.get("result"), dict) else {}
    if res.get("summary"):
        lines.append(f"执行者摘要：{clip(str(res['summary']), 1500)}")
    if res.get("artifact"):
        lines.append(f"产物：{res['artifact']}")
    if leg.get("diff"):
        f, a, d = leg["diff"]
        lines.append(f"本轮改动（不含 .baton/）：{f} 个文件，+{a} −{d}")
        cs = project.config["change_size"]
        if res.get("change_size") == "minor" and (f >= cs["major_files"] or a + d >= cs["major_lines"]):
            lines.append("注意：执行者标为 minor，但改动量超过 major 阈值，可能是未汇报的大修改。")
        if (f or a or d) and not leg.get("log_grew"):
            lines.append("注意：有代码改动但 log.md 本轮没有新增记录。")
    if ev == "REPORT" and leg.get("end_ref"):
        st = read_json(project.state_path(task), {}) or {}
        base = report_baseline(st, leg)
        if base:
            try:
                f, a, d = shortstat(project.root, base, leg["end_ref"])
                lines.append(f"自上次汇报以来（{base}）：{f} 个文件，+{a} −{d}；审阅用 baton diff {task} --since report")
            except Exception:  # noqa: BLE001
                pass
    if leg.get("usage"):
        lines.append(f"token：{fmt_usage(leg['usage'])}")
    lines.append(f"用时：{fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}")
    if leg.get("error"):
        lines.append(f"错误：{leg['error']}")
    if leg.get("stderr_tail"):
        lines.append("stderr 末尾：\n" + leg["stderr_tail"])
    lines.append(f"回滚点：{leg.get('start_ref') or '无'} → {leg.get('end_ref') or '无'}")
    q = safe_quota(block=False)
    if codex_quota.is_low(q):
        lines.append(codex_quota.describe(q).splitlines()[0])
    lines.append("下一步：" + NEXT_STEP.get(ev, "查看 baton status"))
    return "\n".join(lines)


def cmd_wait(project, args):
    project.require_init()
    minutes = args.tick if args.tick is not None else project.config["supervision"]["interval_minutes"]
    tick = float(minutes) * 60
    last_quota_check = 0.0
    while True:
        st = refresh_runner_liveness(project, args.task, project.load_state(args.task))
        leg = current_leg(st)
        if not leg:
            print(f"BATON::EVENT IDLE task={args.task}：还没有启动任何一轮")
            return
        if leg["status"] != "running":
            if leg.get("reported"):
                print(f"BATON::EVENT IDLE task={args.task} leg={leg['n']}：没有运行中的一轮，"
                      f"上一轮状态 {leg['status']}（已处理过）。需要继续就 baton resume。")
                return
            print(leg_event_text(project, args.task, leg), flush=True)
            mark_reported(project, args.task)
            return
        elapsed_sup = now() - st.get("last_supervision_at", leg["started_at"])
        if elapsed_sup >= tick:
            print(f"BATON::EVENT TICK task={args.task} leg={leg['n']}：距上次监管 {fmt_dur(elapsed_sup)}，"
                  f"本轮已运行 {fmt_dur(now() - leg['started_at'])}")
            print("下一步：" + NEXT_STEP["TICK"])
            return
        if now() - last_quota_check > 300:
            last_quota_check = now()
            data = safe_quota(block=True, notify=True)
            keys = codex_quota.low_window_keys(data)
            alerted = st.get("quota_alerted") or []
            fresh = [k for k in keys if not codex_quota.already_alerted(k, alerted)]
            if fresh:
                project.update_state(args.task, lambda s: s.update(quota_alerted=sorted(set((s.get("quota_alerted") or []) + keys))))
                print(f"BATON::EVENT QUOTA_LOW task={args.task} leg={leg['n']}")
                print(codex_quota.describe(data))
                print("下一步：" + NEXT_STEP["QUOTA_LOW"])
                return
        time.sleep(10)


def cmd_status(project, args):
    project.require_init()
    task = args.task
    st = refresh_runner_liveness(project, task, project.load_state(task))
    cfg = project.config
    leg = current_leg(st)
    print(f"# Baton 状态 · {task} · {stamp()}")
    print(f"任务状态：{st.get('status')}；Codex 会话：{st.get('thread_id') or '未知'}；共 {len(st.get('legs', []))} 轮")
    if not leg:
        return
    running = leg["status"] == "running"
    print(f"当前第 {leg['n']} 轮（{leg['kind']}）：{leg['status']}，已运行 {fmt_dur((leg.get('ended_at') or now()) - leg['started_at'])}"
          + (f"，codex pid {leg.get('codex_pid')} {'存活' if codex_alive(leg) else '不在'}" if running else ""))
    if leg["status"] != "running" and not leg.get("reported"):
        print(f"注意：这一轮已结束（{leg['status']}），事件还没被 baton wait 报告过。")
    last_sup = st.get("last_supervision_at", leg["started_at"])
    print(f"上次监管：{stamp(last_sup)}（{fmt_dur(now() - last_sup)} 前）；"
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
    if is_git(project.root):
        print("\n## 代码改动（不含 .baton/ .council/）")
        try:
            now_commit = snapshot(project.root, "baton status probe")
        except Exception as e:  # noqa: BLE001
            print(f"（快照失败，无法统计改动：{e}）")
    if now_commit:
        for label, ref in (("任务起点", st.get("base_ref")), ("本轮起点", leg.get("start_ref")),
                           ("上次监管", sup.get("ref"))):
            if ref:
                f, a, d = shortstat(project.root, ref, now_commit)
                print(f"相对{label} {ref}：{f} 个文件，+{a} −{d}")
        if leg.get("start_ref"):
            stat_out = git(project.root, "diff", "--stat=120", leg["start_ref"], now_commit, "--", ".", *EXCLUDE, check=False)
            print(stat_out.rstrip()[-4000:] or "（本轮暂无改动）")
            pairs = [(s, p) for _, _, s, p in diff_tree_entries(project.root, leg["start_ref"], now_commit)]
            deleted = [p for s, p in pairs if s == "D"]
            if deleted:
                print(f"注意：本轮删除了 {len(deleted)} 个文件（含改名前的旧路径）：" + "，".join(deleted[:20]))
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

    pend = {"leg": leg["n"], "events_offset": events_end, "log_offset": log_end,
            "commit": now_commit, "at": now()}
    project.update_state(task, lambda s: s.update(pending_supervision=pend))


def cmd_supervised(project, args):
    project.require_init()
    task = args.task
    st = project.load_state(task)
    leg = current_leg(st)
    pend = st.get("pending_supervision")
    # Use the baseline of the snapshot Opus actually read, so nothing between status and now is skipped.
    if not (pend and leg and pend.get("leg") == leg["n"] and now() - pend.get("at", 0) < 3600):
        pend = None
    label = f"tick-{stamp(fmt='%Y%m%d-%H%M%S')}"
    ref, err = None, None
    if pend and pend.get("commit"):
        try:
            ref = store_ref(project.root, task, label, pend["commit"])
        except Exception as e:  # noqa: BLE001
            err = str(e)
    else:
        ref, err = try_checkpoint(project.root, task, label)
    events_off = pend["events_offset"] if pend else (read_events(leg["events"])[1] if leg else 0)
    log_off = pend["log_offset"] if pend else file_size(project.p("log.md"))
    since = pend["at"] if pend else now()
    with open(project.p("supervision.md"), "a") as f:
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
    with open(project.leg_path(args.task, leg["n"], "stop"), "w") as f:
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
    leg = current_leg(st)
    if leg and leg["status"] == "running":
        cmd_stop(project, argparse.Namespace(task=args.task, reason="steer"))
        st = project.load_state(args.task)
        leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("当前轮还没停下，稍后用 baton status 确认后再纠偏")
    if leg and not leg.get("reported"):
        if leg["status"] != "stopped":
            surface_unreported(project, args.task, st, "纠偏")
        mark_reported(project, args.task)
    cmd_resume(project, argparse.Namespace(task=args.task, kind="纠偏"), message=message)


def cmd_checkpoint(project, args):
    check_task(args.task)
    label = args.label or f"manual-{stamp(fmt='%Y%m%d-%H%M%S')}"
    if not LABEL_RE.fullmatch(label):
        die("标签只能用字母、数字、下划线和连字符")
    if not is_git(project.root):
        die("非 git 仓库，无法建立回滚点")
    ref, err = try_checkpoint(project.root, args.task, label)
    if err:
        die(f"回滚点没建成：{err}")
    print(ref)


def cmd_refs(project, args):
    out = git(project.root, "for-each-ref", "--sort=creatordate",
              "--format=%(creatordate:format:%m-%d %H:%M)  %(refname)", f"refs/baton/{args.task}", check=False)
    print(out.rstrip() or "（无回滚点）")


def cmd_diff(project, args):
    st = project.load_state(args.task)
    leg = current_leg(st)
    base = {"base": st.get("base_ref"), "leg": leg and leg.get("start_ref"),
            "tick": (st.get("supervision") or {}).get("ref"),
            "report": leg and report_baseline(st, leg) if leg else None}.get(args.since, args.since)
    if not base:
        die("没有对应的回滚点")
    try:
        target = snapshot(project.root, "baton diff probe")
    except Exception as e:  # noqa: BLE001
        die(f"快照失败：{e}")
    extra = ["--stat=120"] if args.stat else []
    sys.stdout.write(git(project.root, "diff", *extra, base, target, "--", ".", *EXCLUDE, check=False))


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


def _backup(top, full, backup_dir):
    dest = os.path.join(backup_dir, os.path.relpath(full, top))
    if os.path.lexists(dest):
        raise RuntimeError(f"备份目标已存在：{dest}")
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


def cmd_rollback(project, args):
    """Restore worktree files under the project root to a checkpoint.

    Only worktree files change: HEAD, branches and the real index stay as they are. The current
    state is checkpointed first, and every file that will be overwritten or deleted is copied
    byte for byte to .baton/rollback-backup/<name>/ before anything changes. Nothing is written
    or deleted through a symlinked directory or inside a submodule.
    """
    root = project.root
    if not is_git(root):
        die("非 git 仓库，无法回滚")
    target = git(root, "rev-parse", "--verify", "-q", f"{args.ref}^{{commit}}", check=False).strip()
    if not target:
        die(f"找不到回滚点 {args.ref}")
    st = project.load_state(args.task)
    leg = current_leg(st)
    if leg and leg["status"] == "running":
        die("执行者仍在运行，先 baton stop")
    if not args.yes:
        print(f"将把 {root} 下的工作区文件（不含 .baton/ .council/）恢复到 {args.ref}。HEAD、分支和暂存区不变。")
        print("当前状态会先存成回滚点，要改写或删除的文件会先原样备份到 .baton/rollback-backup/。子模块内部不在回滚范围内。")
        print("确认后加 --yes 重新执行。")
        sys.exit(3)

    write_baton_gitignore(project)
    top = git_top(root)
    pre_ref, err = try_checkpoint(root, args.task, f"pre-rollback-{stamp(fmt='%Y%m%d-%H%M%S')}")
    if err:
        die(f"回滚前的状态没能存下来，已放弃回滚：{err}")
    pre = git(root, "rev-parse", pre_ref).strip()
    backup_dir = project.p("rollback-backup", os.path.basename(pre_ref))
    entries = diff_tree_entries(root, pre, target)
    gitlinks = set()
    for tree in (pre, target):
        for line in git(top, "ls-tree", "-r", "-z", tree).split("\0"):
            if line.startswith("160000 "):
                gitlinks.add(line.split("\t", 1)[1])
    deletes, writes, skipped = [], [], []
    for src, dst, status_, path in entries:
        if "160000" in (src, dst) or _under(path, gitlinks):
            skipped.append(f"{path}（子模块）")
        elif _leading_symlink(top, path):
            skipped.append(f"{path}（路径经过符号链接）")
        elif status_ == "D":
            deletes.append(path)
        elif status_ in ("A", "M", "T"):
            writes.append(path)

    # 0. back up every existing path that will be deleted or overwritten, before changing anything
    backed, seen = [], set()
    try:
        for path in deletes + writes:
            full = os.path.join(top, path)
            if os.path.lexists(full) and not (os.path.isdir(full) and not os.path.islink(full)):
                st_ = os.lstat(full)
                if (st_.st_dev, st_.st_ino) in seen:
                    continue  # same file under another name (case-insensitive disk)
                seen.add((st_.st_dev, st_.st_ino))
                backed.append(_backup(top, full, backup_dir))
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
                    backed.append(_backup(top, full_lead, backup_dir))
                    os.unlink(full_lead)
                    break
            full = os.path.join(top, path)
            if os.path.isdir(full) and not os.path.islink(full):
                if os.path.lexists(os.path.join(full, ".git")):
                    raise RuntimeError(f"{path} 是一个嵌套的 git 仓库，不会移动它")
                dest = os.path.join(backup_dir, path)
                if os.path.lexists(dest):
                    raise RuntimeError(f"备份目标已存在：{dest}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(full, dest)
                backed.append(dest)
        # 3. write the target versions through a throwaway index (git applies modes and filters);
        #    .gitattributes first so the other files are converted with the target's attributes
        if writes:
            fd, tmp_index = tempfile.mkstemp(prefix="baton-index-")
            os.close(fd)
            os.remove(tmp_index)
            try:
                env = dict(os.environ, GIT_INDEX_FILE=tmp_index)
                git(top, "read-tree", target, env=env)
                attrs = [p for p in writes if p.split("/")[-1] == ".gitattributes"]
                rest = [p for p in writes if p not in attrs]
                for batch in (attrs, rest):
                    if batch:
                        git(top, "checkout-index", "-f", "-z", "--stdin", env=env,
                            input_text="".join(p + "\0" for p in batch))
            finally:
                if os.path.exists(tmp_index):
                    os.remove(tmp_index)
            written = writes
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
    if gitlinks:
        print("注意：项目里有子模块或嵌套仓库，它们内部的改动不在回滚范围内。")
    staged = git(root, "diff", "--cached", "--name-only", "--", ".", *EXCLUDE, check=False).split()
    if staged:
        print("注意：暂存区里还有这些改动（回滚不碰暂存区）：" + "，".join(staged[:20])
              + "。如需丢弃，问过用户后运行 git restore --staged -- <路径>")


def cmd_list(project, args):
    project.require_init()
    for task in project.tasks():
        st = read_json(project.state_path(task), {}) or {}
        leg = current_leg(st) if st else None
        print(f"{task:<24} {st.get('status', 'draft'):<8} 轮次 {len(st.get('legs', []))}"
              + (f"  最新：{leg['status']} @ {stamp(leg['started_at'])}" if leg else ""))


def cmd_config(project, args):
    cfg = project.config
    judge = cfg["judge"]
    ex = cfg["executor"]
    print(f"BATON::CONFIG judge={judge['model']} executor={ex['model']} effort={ex['reasoning_effort']}")
    if args.json:
        print(json.dumps(cfg, ensure_ascii=False, indent=2))
        return
    print(f"judge.model：{judge['model']}")
    print(f"judge.council_for_major_decisions：{judge.get('council_for_major_decisions')}")
    for key in ("model", "reasoning_effort", "service_tier", "disable_fast_mode", "network_access", "sandbox"):
        print(f"executor.{key}：{ex.get(key)}")
    print(f"supervision.test_command：{cfg['supervision'].get('test_command', '')}")
    print("配置层：")
    for path, exists in project.config_paths():
        print(f"- {path}：{'存在' if exists else '缺失'}")


def cmd_quota(project, args):
    argv = (["--json"] if args.json else []) + (["--refresh"] if args.refresh else []) + ["--notify"]
    sys.exit(codex_quota.main(argv))


COUNCIL_BUILTIN = ["council-cc-glm", "council-codex-gpt", "council-cc-kimi"]  # council.sh's own fallback


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
    codex = shutil.which("codex")
    line("OK" if codex else "FAIL", f"codex CLI：{codex or '未安装'}")
    if codex:
        line("OK", run(["codex", "--version"], check=False).strip())
    try:
        cfg = project.config
    except ConfigError as e:
        line("FAIL", str(e))
        sys.exit(1)
    ex = cfg["executor"]
    baton_state = "存在" if os.path.exists(project.baton_config_path) else "缺失"
    line("OK" if baton_state == "存在" else "WARN",
         f"judge 模型：{cfg['judge']['model']}；baton.json：{baton_state}（{project.baton_config_path}）")
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
    launcher = os.path.realpath(os.path.join(SKILL_DIR, "bin", "baton"))
    on_path = shutil.which("baton")
    if not on_path:
        line("WARN", f"PATH 上没有 baton；可以直接用 {launcher}")
    elif os.path.realpath(on_path) != launcher:
        line("WARN", f"PATH 上的 baton（{on_path}）不是这个 skill 的，请改用 {launcher} 或处理同名冲突")
    else:
        line("OK", f"baton 命令：{on_path}")
    council = os.path.join(council_dir(), "scripts", "launch.sh")
    line("OK" if os.path.exists(council) else "WARN",
         "council skill：" + ("已安装" if os.path.exists(council) else f"未安装（{council_dir()}，./install.sh --council）"))
    if os.path.exists(council):
        line(*council_check())
    line("OK" if is_git(project.root) else "WARN", f"项目 {project.root}：" + ("git 仓库" if is_git(project.root) else "非 git，无回滚点"))
    line("OK" if os.path.isdir(project.dir) else "WARN", ".baton/：" + ("已初始化" if os.path.isdir(project.dir) else "未初始化（baton init）"))
    sl = statusline_report(project).splitlines()[0]
    good = "已安装 Baton 版本" in sl
    line("OK" if good else "WARN", sl + ("" if good else "；baton init 或 baton statusline install"))
    if good and is_git(project.root) and (settings_tracked(project) or not settings_ignored(project)):
        line("WARN", ".claude/settings.local.json 没有被 git 忽略，里面的本机路径可能被提交")
    sys.exit(0 if ok else 1)


def main():
    ap = argparse.ArgumentParser(prog="baton", description="Claude 指挥、Codex 执行的工作流工具")
    ap.add_argument("--root", help="项目目录（默认：当前目录所在 git 仓库根）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init", help="在项目里创建 .baton/，并配置带 Codex 额度的 statusline")
    s.add_argument("--no-statusline", action="store_true", help="不改项目的 statusline")
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
    s = sub.add_parser("wait", help="阻塞到本轮结束、监管时间到或额度告警（请在后台运行）")
    s.add_argument("task")
    s.add_argument("--tick", type=float, help="监管间隔分钟数（默认取配置）")
    s = sub.add_parser("status", help="监管快照")
    s.add_argument("task")
    s = sub.add_parser("supervised", help="记录一次监管结论并重置计时")
    s.add_argument("task")
    s.add_argument("--verdict", required=True, choices=["on_track", "drifting", "breaking"])
    s.add_argument("--note", required=True)
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
    s.add_argument("--since", default="leg", help="base / leg / tick / report / 任意 ref")
    s.add_argument("--stat", action="store_true")
    s = sub.add_parser("rollback", help="把工作区文件恢复到某个回滚点（先自动保存当前状态）")
    s.add_argument("task")
    s.add_argument("ref")
    s.add_argument("--yes", action="store_true")
    sub.add_parser("list", help="列出任务")
    s = sub.add_parser("config", help="显示生效配置")
    s.add_argument("--json", action="store_true", help="输出完整合并后的 JSON 配置")
    s = sub.add_parser("quota", help="Codex 额度")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true")
    sub.add_parser("doctor", help="环境自检")
    s = sub.add_parser("_run")
    s.add_argument("task")
    s.add_argument("n")
    args = ap.parse_args()
    project = Project(args.root or find_root(os.getcwd()))
    handler = {
        "init": cmd_init, "statusline": cmd_statusline, "new": cmd_new, "start": cmd_start,
        "resume": cmd_resume, "steer": cmd_steer, "wait": cmd_wait, "status": cmd_status,
        "supervised": cmd_supervised, "stop": cmd_stop, "checkpoint": cmd_checkpoint, "refs": cmd_refs,
        "diff": cmd_diff, "rollback": cmd_rollback, "list": cmd_list, "config": cmd_config, "quota": cmd_quota,
        "doctor": cmd_doctor, "_run": cmd_run,
    }[args.cmd]
    if args.cmd == "steer":
        args.kind = "纠偏"
    try:
        handler(project, args)
    except ConfigError as e:
        die(str(e))


if __name__ == "__main__":
    main()
