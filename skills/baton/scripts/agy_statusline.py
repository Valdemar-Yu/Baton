#!/usr/bin/env python3
"""Antigravity CLI (`agy`) custom statusline synced from cc-statusline + Baton.

Layout:
  Line 1: 🤖 <model> [1M] ⚡<effort>  🧠 <bar> <used%> (<used>/<size>)  ✨ Gemini 5h <rem%> ↻<reset> · 7d <rem%> ↻<reset>  🕐 <clock>
  Line 2: 🟠 Opus/3P 5h <rem%> ↻<reset> · 7d <rem%> ↻<reset>  [│  🏃 Baton ...]
  Line 3 (only if inside a Baton project and Line 2 would overflow terminal width): 🏃 Baton ...

Reads the JSON session payload piped by `agy` on stdin and prints ANSI-styled
lines to stdout. Never crashes; missing fields degrade silently.
"""
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import unicodedata
from datetime import datetime


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")
_EFFORT_PARENS_RE = re.compile(
    r"\s*\((xhigh|extra[- ]?high|extra|high|medium|low|max|ultra|minimal)\)\s*$",
    re.I,
)
_EFFORT_SUFFIX_RE = re.compile(
    r"[-_](xhigh|extra[-_]?high|high|medium|low|max|ultra|minimal)$",
    re.I,
)


def c(txt, code):
    return f"\033[{code}m{txt}\033[0m"


def safe_text(value):
    return "".join(
        ch for ch in str(value)
        if ord(ch) >= 32 and not 127 <= ord(ch) < 160
    )


def disp_width(s):
    s = _ANSI_RE.sub("", s)
    w = 0
    for ch in s:
        o = ord(ch)
        if unicodedata.combining(ch) or 0xFE00 <= o <= 0xFE0F:
            continue
        if (
            unicodedata.east_asian_width(ch) in ("W", "F")
            or 0x1F000 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x27BF
            or 0x2300 <= o <= 0x23FF
            or 0x2B00 <= o <= 0x2BFF
        ):
            w += 2
        else:
            w += 1
    return w


def term_width(data):
    tw = data.get("terminal_width")
    if isinstance(tw, (int, float)) and not isinstance(tw, bool) and tw >= 20:
        return int(tw)
    try:
        return int(os.environ["COLUMNS"])
    except (KeyError, ValueError):
        return shutil.get_terminal_size((100, 24)).columns


def human(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def bar(pct, width=8):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(pct / 100.0 * width))
    return "█" * filled + "░" * (width - filled)


def pct_color(remaining):
    if remaining > 50:
        return "32"
    if remaining > 20:
        return "33"
    return "31"


def used_color(used):
    if used < 60:
        return "32"
    if used < 85:
        return "33"
    return "31"


def _parse_iso(s):
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        return datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fmt_duration_sec(seconds):
    try:
        delta = int(round(float(seconds)))
    except Exception:
        return ""
    if delta <= 0:
        return "now"
    d, rem = divmod(delta, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m"


def fmt_countdown(resets_at=None, reset_in_seconds=None):
    if reset_in_seconds is not None and not isinstance(reset_in_seconds, bool):
        try:
            sec = float(reset_in_seconds)
            if math.isfinite(sec):
                return fmt_duration_sec(sec)
        except (TypeError, ValueError):
            pass
    if resets_at is not None and not isinstance(resets_at, bool):
        ts = None
        if isinstance(resets_at, (int, float)):
            ts = float(resets_at)
            if ts > 1e12:  # milliseconds
                ts /= 1000.0
        elif isinstance(resets_at, str):
            ts = _parse_iso(resets_at)
        if ts is not None:
            return fmt_duration_sec(ts - time.time())
    return ""


def _read_agy_settings():
    path = os.path.expanduser("~/.gemini/antigravity-cli/settings.json")
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _normalize_effort(val):
    if not val:
        return ""
    s = safe_text(val).strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {
        "extra-high": "xhigh",
        "extrahigh": "xhigh",
        "extra": "xhigh",
    }
    return aliases.get(s, s)


def extract_model_and_effort(data, ctx):
    model = data.get("model")
    raw_name = ""
    raw_id = ""
    model_effort = ""
    if isinstance(model, dict):
        raw_name = safe_text(
            model.get("display_name")
            or model.get("displayName")
            or model.get("name")
            or model.get("id")
            or ""
        ).strip()
        raw_id = safe_text(model.get("id") or model.get("model_id") or "").strip()
        model_effort = _normalize_effort(
            model.get("effort")
            or model.get("reasoning_effort")
            or model.get("thinking_level")
        )
    elif isinstance(model, str) and model.strip():
        raw_name = safe_text(model).strip()
        raw_id = raw_name

    if not raw_name:
        fallback_model = safe_text(
            data.get("model_name")
            or data.get("model_id")
            or _read_agy_settings().get("model")
            or "Antigravity"
        ).strip()
        raw_name = fallback_model
        if not raw_id:
            raw_id = fallback_model

    # Extract explicit effort field if present
    effort_obj = data.get("effort")
    if isinstance(effort_obj, dict):
        effort = _normalize_effort(effort_obj.get("level") or effort_obj.get("value"))
    else:
        effort = _normalize_effort(effort_obj)
    if not effort:
        effort = _normalize_effort(
            data.get("reasoning_effort")
            or data.get("thinking_level")
            or model_effort
        )

    # Extract embedded effort from display name like "Gemini 4 Neon (XHigh)" or id "...-xhigh"
    m = _EFFORT_PARENS_RE.search(raw_name)
    if m:
        if not effort:
            effort = _normalize_effort(m.group(1))
        raw_name = _EFFORT_PARENS_RE.sub("", raw_name).strip()
    else:
        m2 = _EFFORT_SUFFIX_RE.search(raw_id)
        if m2 and not effort:
            effort = _normalize_effort(m2.group(1))

    # Strip any "(1M context)" tag and re-add compact "[1M]" when effective window >= 1M
    is_1m = False
    if ctx:
        is_1m = ctx[1] >= 1_000_000
    else:
        is_1m = "1m" in raw_id.lower() or "1m" in raw_name.lower()
    clean_name = re.sub(r"\s*[\(\[][^)\]]*1m[^)\]]*[\)\]]", "", raw_name, flags=re.I).strip()
    display = f"{clean_name} [1M]" if is_1m else clean_name
    return display or "Antigravity", effort, raw_id.lower() or clean_name.lower()


def extract_context(data):
    cw = data.get("context_window") or data.get("context") or {}
    if not isinstance(cw, dict):
        return None
    size = cw.get("context_window_size") or cw.get("max_tokens") or cw.get("limit")
    try:
        size = int(size) if size else None
    except (TypeError, ValueError):
        size = None

    used_pct = cw.get("used_percentage")
    if used_pct is None and cw.get("remaining_percentage") is not None:
        try:
            used_pct = 100.0 - float(cw["remaining_percentage"])
        except (TypeError, ValueError):
            used_pct = None

    cur = cw.get("current_usage") or {}
    cur_tokens = None
    if isinstance(cur, dict) and cur:
        vals = [
            cur.get("input_tokens"),
            cur.get("cache_read_input_tokens"),
            cur.get("cache_creation_input_tokens"),
        ]
        if any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            cur_tokens = int(sum(v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)))

    total_in = cw.get("total_input_tokens")
    if not isinstance(total_in, (int, float)) or isinstance(total_in, bool):
        total_in = None

    if used_pct is not None:
        try:
            used_pct = max(0.0, min(100.0, float(used_pct)))
        except (TypeError, ValueError):
            used_pct = None

    if used_pct is None and size and cur_tokens is not None:
        used_pct = max(0.0, min(100.0, cur_tokens / size * 100.0))
    elif used_pct is None and size and total_in is not None:
        used_pct = max(0.0, min(100.0, total_in / size * 100.0))

    if used_pct is None:
        return None

    if size:
        if cur_tokens is not None and 0 <= cur_tokens <= size:
            used_tok = cur_tokens
        else:
            used_tok = int(round(size * used_pct / 100.0))
        return used_pct, size, used_tok
    return used_pct, 0, int(cur_tokens or total_in or 0)


def _extract_bucket_remaining_and_reset(bucket):
    if not isinstance(bucket, dict):
        return None
    rem = None
    if bucket.get("remaining_fraction") is not None:
        try:
            frac = float(bucket["remaining_fraction"])
            rem = frac * 100.0 if frac <= 1.0 else frac
        except (TypeError, ValueError):
            rem = None
    if rem is None:
        for k in ("remaining_percentage", "remaining_percent", "remaining"):
            if bucket.get(k) is not None:
                try:
                    rem = float(bucket[k])
                    break
                except (TypeError, ValueError):
                    pass
    if rem is None:
        for k in ("used_percentage", "used_percent"):
            if bucket.get(k) is not None:
                try:
                    rem = 100.0 - float(bucket[k])
                    break
                except (TypeError, ValueError):
                    pass
    cd = fmt_countdown(
        resets_at=bucket.get("reset_time") or bucket.get("resets_at") or bucket.get("resetsAt"),
        reset_in_seconds=bucket.get("reset_in_seconds") or bucket.get("resetInSeconds"),
    )
    # Protobuf v3 omits zero-valued float fields (`remaining_fraction: 0.0`) on exhaustion;
    # if a valid reset timestamp/countdown is present, treat missing fraction as 0%.
    if rem is None:
        if cd:
            rem = 0.0
        else:
            return None
    if not math.isfinite(rem):
        return None
    rem = max(0.0, min(100.0, rem))
    return rem, cd


def _collect_quota_buckets(data):
    buckets = {}
    q = data.get("quota")
    if isinstance(q, dict):
        for k, v in q.items():
            if isinstance(v, dict):
                buckets[str(k).lower()] = v
    elif isinstance(q, list):
        for item in q:
            if isinstance(item, dict):
                name = str(item.get("id") or item.get("name") or item.get("bucket") or "").lower()
                if name:
                    buckets[name] = item

    rl = data.get("rate_limits")
    if isinstance(rl, dict):
        for k, v in rl.items():
            if isinstance(v, dict) and str(k).lower() not in buckets:
                buckets[str(k).lower()] = v
    return buckets


def _pick_bucket(buckets, exact_keys, family_tokens=None, window_tokens=None, exclude_tokens=()):
    for key in exact_keys:
        if key in buckets:
            parsed = _extract_bucket_remaining_and_reset(buckets[key])
            if parsed is not None:
                return parsed
    if family_tokens and window_tokens:
        for key, val in buckets.items():
            if any(ex in key for ex in exclude_tokens):
                continue
            if any(ft in key for ft in family_tokens) and any(wt in key for wt in window_tokens):
                parsed = _extract_bucket_remaining_and_reset(val)
                if parsed is not None:
                    return parsed
    return None


def extract_quota_pools(data):
    """Extract independent Gemini and Opus/3P quota pools from `agy` statusLine payload.

    Returns dict with keys `"gemini"` and `"opus"`, each mapping to a list of
    `(label, remaining_pct, countdown)` tuples for 5h / 7d windows.
    """
    buckets = _collect_quota_buckets(data)
    if not buckets:
        return {"gemini": [], "opus": []}

    gemini_5h = _pick_bucket(
        buckets,
        exact_keys=("gemini-5h", "gemini_5h", "gemini-five-hour", "gemini-rolling"),
        family_tokens=("gemini",),
        window_tokens=("5h", "five_hour", "five-hour", "rolling"),
        exclude_tokens=("argon", "3p", "claude", "opus", "gpt"),
    )
    gemini_7d = _pick_bucket(
        buckets,
        exact_keys=("gemini-weekly", "gemini_weekly", "gemini-7d", "gemini_7d", "gemini-seven-day"),
        family_tokens=("gemini",),
        window_tokens=("weekly", "7d", "seven_day", "seven-day"),
        exclude_tokens=("argon", "3p", "claude", "opus", "gpt"),
    )

    opus_5h = _pick_bucket(
        buckets,
        exact_keys=("3p-5h", "3p_5h", "claude-5h", "claude_5h", "opus-5h", "claude-gpt-5h", "partner-5h"),
        family_tokens=("3p", "claude", "opus", "partner"),
        window_tokens=("5h", "five_hour", "five-hour", "rolling"),
    )
    opus_7d = _pick_bucket(
        buckets,
        exact_keys=(
            "3p-weekly", "3p_weekly", "3p-7d", "3p_7d",
            "claude-weekly", "claude_weekly", "claude-7d", "claude_7d",
            "opus-weekly", "opus_weekly", "claude-gpt-weekly", "partner-weekly",
        ),
        family_tokens=("3p", "claude", "opus", "partner"),
        window_tokens=("weekly", "7d", "seven_day", "seven-day"),
    )

    # Generic fallback when only unnamed windows (five_hour / seven_day / 5h / weekly) exist
    if gemini_5h is None and opus_5h is None:
        gemini_5h = _pick_bucket(buckets, exact_keys=("five_hour", "five-hour", "5h", "rolling", "primary"))
    if gemini_7d is None and opus_7d is None:
        gemini_7d = _pick_bucket(buckets, exact_keys=("seven_day", "seven-day", "weekly", "7d", "secondary"))

    gemini_out = []
    if gemini_5h is not None:
        gemini_out.append(("5h", gemini_5h[0], gemini_5h[1]))
    if gemini_7d is not None:
        gemini_out.append(("7d", gemini_7d[0], gemini_7d[1]))

    opus_out = []
    if opus_5h is not None:
        opus_out.append(("5h", opus_5h[0], opus_5h[1]))
    if opus_7d is not None:
        opus_out.append(("7d", opus_7d[0], opus_7d[1]))

    return {"gemini": gemini_out, "opus": opus_out}


def _format_quota_pool(title, title_color, windows):
    if not windows:
        return ""
    segs = []
    for label, rem, cd in windows:
        txt = f"{label} " + c(f"{rem:.0f}%", pct_color(rem))
        if cd:
            txt += c(f" ↻{cd}", "90")
        segs.append(txt)
    return c(title, title_color) + " " + c(" · ", "90").join(segs)


def extract_quota_windows(data, model_hint=""):
    """Legacy helper returning (label, emoji, remaining_pct, countdown) for primary pool."""
    pools = extract_quota_pools(data)
    hint = (model_hint or "").lower()
    use_opus = any(k in hint for k in ("claude", "opus", "sonnet", "haiku", "3p"))
    chosen = pools["opus"] if (use_opus and pools["opus"]) else (pools["gemini"] or pools["opus"])
    return [(lbl, "⏳" if lbl == "5h" else "📅", rem, cd) for lbl, rem, cd in chosen]


# ---- Baton status integration ----

def _project_dir_of(data):
    ws = data.get("workspace") or {}
    if isinstance(ws, dict):
        cand = ws.get("project_dir") or ws.get("current_dir")
        if cand:
            return cand
    return data.get("cwd") or os.getcwd()


def _baton_home_real():
    home = os.environ.get("BATON_HOME") or "~/.baton"
    if "${" in home:
        home = "~/.baton"
    return os.path.realpath(os.path.abspath(os.path.expanduser(home)))


def _is_baton_project_dir(cur):
    if os.path.isfile(os.path.join(cur, "baton.json")):
        return True
    dot_baton = os.path.join(cur, ".baton")
    if os.path.isdir(dot_baton) and os.path.realpath(dot_baton) != _baton_home_real():
        if (
            os.path.isfile(os.path.join(dot_baton, "config.json"))
            or os.path.isdir(os.path.join(dot_baton, "tasks"))
        ):
            return True
    return False


def _find_baton_root(start):
    cur = os.path.abspath(os.path.expanduser(start or os.getcwd()))
    while True:
        if _is_baton_project_dir(cur):
            return cur
        if os.path.exists(os.path.join(cur, ".git")):
            return None
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _project_id(project_dir):
    real = os.path.realpath(project_dir)
    return os.path.basename(real) + "-" + hashlib.sha1(os.fsencode(real)).hexdigest()[:8]


def _read_json_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _baton_tasks_dirs(project_dir):
    dirs = [os.path.join(project_dir, ".baton", "tasks")]
    home = os.environ.get("BATON_HOME") or "~/.baton"
    if "${" not in home:
        home = os.path.abspath(os.path.expanduser(home))
        dirs.append(os.path.join(home, "projects", _project_id(project_dir), "tasks"))
    return dirs


def _baton_running_and_latest(project_dir):
    running = None
    latest = None
    for tasks_dir in _baton_tasks_dirs(project_dir):
        try:
            names = sorted(os.listdir(tasks_dir))
        except OSError:
            continue
        for task in names:
            path = os.path.join(tasks_dir, task, "state.json")
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            state = _read_json_file(path)
            if not state:
                continue
            legs = state.get("legs") or []
            leg = legs[-1] if legs else {}
            monitor = leg.get("monitor") or {}
            advisor = state.get("advisor_active") or {}
            if running is None and (
                leg.get("status") == "running"
                or monitor.get("status") == "running"
                or (isinstance(advisor, dict) and advisor.get("status") == "running")
            ):
                model = leg.get("model") or (leg.get("executor") or {}).get("model") or "?"
                effort = leg.get("reasoning_effort") or ""
                backend = leg.get("backend") or ""
                running = {
                    "task": task,
                    "leg": leg.get("n", len(legs)),
                    "backend": backend,
                    "model": model,
                    "effort": effort,
                    "escalated": bool(leg.get("escalated")),
                    "monitor": monitor.get("status") == "running",
                    "advisor": isinstance(advisor, dict) and advisor.get("status") == "running",
                    "capacity_wait": leg.get("capacity_wait"),
                }
            status = leg.get("status") or state.get("status") or "draft"
            item = (mtime, task, leg.get("n", len(legs)), status)
            if latest is None or item[0] > latest[0]:
                latest = item
    return running, latest


def baton_line(data):
    root = _find_baton_root(_project_dir_of(data))
    if not root:
        return None
    cfg = _read_json_file(os.path.join(root, "baton.json"))
    running, latest = _baton_running_and_latest(root)
    version = cfg.get("version")
    ver_tag = f"v{version}" if version else ("v3" if (cfg.get("advisor") or (cfg.get("executor") or {}).get("backend") == "claude") else "v2")
    mode = cfg.get("mode") or "standard"
    preset = cfg.get("preset") if isinstance(cfg.get("preset"), str) and cfg.get("preset") else ""

    if running:
        backend_prefix = f"{running['backend']}:" if running.get("backend") else ""
        effort_suffix = f"⚡{running['effort']}" if running.get("effort") else ""
        model_str = f"{backend_prefix}{running['model']}{effort_suffix}" + ("（升级）" if running.get("escalated") else "")
        parts = [f"🏃 Baton {ver_tag} {running['task']}·第{running['leg']}轮·{model_str}"]
        if running.get("advisor"):
            parts.append("advisor 审阅中")
        if running.get("monitor"):
            parts.append("monitor 运行中")
        elif (cfg.get("monitor") or {}).get("enabled", True) is False or mode == "lean":
            parts.append("monitor 关闭")
        else:
            parts.append("monitor 待命")
        wait = running.get("capacity_wait")
        if isinstance(wait, dict) and wait.get("next_retry_at"):
            rem = max(0, int(float(wait["next_retry_at"]) - time.time()))
            parts.append(f"容量等待 {rem // 60}m")
        return c(" │ ".join(parts), "35")

    head = f"🏃 Baton {ver_tag}·{mode}" + (f"·{preset}" if preset else "")
    ex = cfg.get("executor") or {}
    ex_backend = ex.get("backend")
    ex_model = ex.get("model")
    ex_effort = ex.get("reasoning_effort")
    parts = [head]
    if ex_model:
        if ex_backend == "claude" and ex.get("claude_bin"):
            b_tag = f"{ex['claude_bin']}:"
        else:
            b_tag = f"{ex_backend}:" if ex_backend else ""
        e_tag = f"·{ex_effort}" if ex_effort and ex_backend != "agy" else ""
        parts.append(f"exec {b_tag}{ex_model}{e_tag}")
    parts.append("空闲")
    if latest:
        parts.append(f"最近 {latest[1]}·第{latest[2]}轮·{latest[3]}")
    return c(" │ ".join(parts), "35")


def _maybe_dump_debug(raw_bytes):
    if os.environ.get("AGY_STATUSLINE_DEBUG") != "1":
        return
    try:
        cache_dir = os.path.expanduser("~/.cache/agy-statusline")
        os.makedirs(cache_dir, mode=0o700, exist_ok=True)
        with open(os.path.join(cache_dir, "last-payload.json"), "wb") as f:
            f.write(raw_bytes)
    except Exception:
        pass


def render(data):
    ctx = extract_context(data)
    model_display, effort, _model_hint = extract_model_and_effort(data, ctx)
    tw = term_width(data)

    model_seg = c(f"🤖 {model_display}", "1;36")
    if effort:
        model_seg += " " + c(f"⚡{effort}", "1;38;5;164")
    parts = [model_seg]

    if ctx:
        used_pct, size, used_tok = ctx
        if size:
            size_label = "1M" if size >= 1_000_000 else f"{size // 1000}k"
            ctx_seg = (
                c("🧠 " + bar(used_pct), used_color(used_pct))
                + f" {used_pct:.0f}% "
                + c(f"({human(used_tok)}/{size_label})", "90")
            )
        else:
            ctx_seg = c("🧠 " + bar(used_pct), used_color(used_pct)) + f" {used_pct:.0f}%"
        parts.append(ctx_seg)

    pools = extract_quota_pools(data)
    gemini_seg = _format_quota_pool("✨ Gemini", "1;34", pools["gemini"])
    if gemini_seg:
        parts.append(gemini_seg)

    # Responsive clock on Line 1
    sep = "  "
    base_w = disp_width(sep.join(parts))
    lt = time.localtime()
    variants = [
        time.strftime("%y-%m-%d %H:%M", lt),
        time.strftime("%m-%d %H:%M", lt),
        time.strftime("%H:%M", lt),
    ]
    avail = tw - base_w - disp_width(sep) - 1
    for v in variants:
        seg_txt = f"🕐 {v}"
        if disp_width(seg_txt) <= avail:
            parts.append(c(seg_txt, "90"))
            break

    lines = [sep.join(parts)]

    opus_seg = _format_quota_pool("🟠 Opus/3P", "1;38;5;208", pools["opus"])
    b_line = baton_line(data)
    if opus_seg and b_line:
        divider = c("  │  ", "90")
        combined = opus_seg + divider + b_line
        if disp_width(combined) <= tw - 1:
            lines.append(combined)
        else:
            lines.append(opus_seg)
            lines.append(b_line)
    elif opus_seg:
        lines.append(opus_seg)
    elif b_line:
        lines.append(b_line)

    return "\n".join(lines)


def main():
    raw = sys.stdin.buffer.read()
    _maybe_dump_debug(raw)
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    sys.stdout.write(render(data) + "\n")


if __name__ == "__main__":
    main()
