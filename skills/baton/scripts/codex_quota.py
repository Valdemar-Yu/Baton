#!/usr/bin/env python3
"""Codex quota reader for Baton.

Live source: `codex app-server` JSON-RPC `account/rateLimits/read`. It only reads account
state, so it spends no model tokens. Fallback: the newest `rate_limits` record in
~/.codex/sessions rollout files (whatever the last Codex turn reported).

Results are cached in ~/.cache/baton/codex-quota.json so the statusline never blocks.
Quota is per account, so the `quota` settings come from the skill's config.json only.

CLI:
  codex_quota.py [--json] [--refresh] [--notify] [--max-age SEC] [--quiet]
Exit codes: 0 ok, 10 remaining below the warn threshold, 2 quota unavailable.
A low reading also prints a line starting with `BATON::QUOTA_LOW`.
"""
import calendar
import glob
import json
import os
import select
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # non-POSIX: run without locking
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
CACHE_DIR = os.path.expanduser(os.environ.get("BATON_CACHE_DIR", "~/.cache/baton"))
CACHE_FILE = os.path.join(CACHE_DIR, "codex-quota.json")
LOCK_FILE = os.path.join(CACHE_DIR, "codex-quota.lock")
SPAWN_MARK = os.path.join(CACHE_DIR, "codex-quota.spawned")
LIVE_FAIL_MARK = os.path.join(CACHE_DIR, "codex-quota.live-failed")
NOTIFY_FILE = os.path.join(CACHE_DIR, "codex-quota-notified.json")
CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
CODEX_BIN = os.environ.get("BATON_CODEX_BIN", "codex")


def skill_config():
    try:
        with open(os.path.join(SKILL_DIR, "config.json")) as f:
            return json.load(f)
    except Exception:
        return {}


def warn_threshold():
    env = os.environ.get("BATON_QUOTA_WARN_PERCENT")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return float(skill_config().get("quota", {}).get("warn_remaining_percent", 5))


def cache_seconds():
    return int(skill_config().get("quota", {}).get("cache_seconds", 120))


def _get(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _label(minutes):
    if not minutes:
        return "?"
    minutes = int(minutes)
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def normalize(rl, source, extra=None, observed_at=None):
    """Turn a live (camelCase) or rollout (snake_case) rate-limit snapshot into one shape."""
    extra = extra or {}
    windows = []
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict):
            continue
        used = _get(w, "usedPercent", "used_percent")
        try:
            used = float(used)
        except (TypeError, ValueError):
            continue
        mins = _get(w, "windowDurationMins", "window_minutes")
        windows.append({
            "name": key,
            "label": _label(mins),
            "window_minutes": mins,
            "used": float(used),
            "remaining": max(0.0, 100.0 - float(used)),
            "resets_at": _get(w, "resetsAt", "resets_at"),
        })
    credits = rl.get("credits") or {}
    now = time.time()
    return {
        "source": source,
        "fetched_at": now,
        "observed_at": observed_at or now,
        "plan": _get(rl, "planType", "plan_type"),
        "windows": windows,
        "remaining": min((w["remaining"] for w in windows), default=None),
        "credits": {
            "has": _get(credits, "hasCredits", "has_credits"),
            "unlimited": _get(credits, "unlimited"),
            "balance": _get(credits, "balance"),
        },
        "limit_reached": _get(rl, "rateLimitReachedType", "rate_limit_reached_type"),
        "reset_credits": extra.get("reset_credits"),
        "usage_allowed": extra.get("usage_allowed"),
    }


def apply_resets(data, now=None):
    """Mark windows whose reset time has passed: their old usage no longer counts."""
    if not data:
        return data
    now = now or time.time()
    out = dict(data)
    windows = []
    for w in data.get("windows", []):
        w = dict(w)
        w["reset"] = bool(w.get("resets_at")) and w["resets_at"] <= now
        windows.append(w)
    out["windows"] = windows
    current = [w["remaining"] for w in windows if not w["reset"]]
    out["remaining"] = min(current) if current else None
    return out


def fetch_live(timeout=15.0):
    """Ask a short-lived `codex app-server` for the account's rate limits."""
    try:
        proc = subprocess.Popen([CODEX_BIN, "app-server"], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    fd = proc.stdout.fileno()
    buf = b""

    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode())
        proc.stdin.flush()

    def wait_for(msg_id, deadline):
        # Raw reads from the fd with our own line buffer: select() never misses a response
        # that arrived in the same chunk as a notification.
        nonlocal buf
        while True:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == msg_id and "method" not in msg and ("result" in msg or "error" in msg):
                    return msg
            left = deadline - time.time()
            if left <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], min(0.5, left))
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return None
                buf += chunk
            elif proc.poll() is not None:
                return None

    try:
        deadline = time.time() + timeout
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "baton", "title": "Baton", "version": "0.1"}}})
        if not wait_for(1, deadline):
            return None
        send({"jsonrpc": "2.0", "method": "initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read"})
        msg = wait_for(2, deadline)
        result = (msg or {}).get("result")
        if not isinstance(result, dict) or not isinstance(result.get("rateLimits"), dict):
            return None
        resets = result.get("rateLimitResetCredits")
        resets = resets if isinstance(resets, dict) else {}
        data = normalize(result["rateLimits"], "live", {
            "reset_credits": resets.get("availableCount"),
            "usage_allowed": result.get("ordinaryUsageAllowed"),
        })
        data["limit_id"] = result["rateLimits"].get("limitId")
        return data
    except Exception:  # noqa: BLE001 — any surprise in the reply just means "no live reading"
        return None
    finally:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _record_time(obj, fallback):
    ts = obj.get("timestamp")
    if isinstance(ts, str) and len(ts) >= 19:
        try:
            return calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            pass
    return fallback


def fetch_rollout(max_files=12, limit_id=None):
    """Newest rate_limits reading for one limit bucket from recently written rollout files."""
    limit_id = limit_id or (load_cache() or {}).get("limit_id") or "codex"
    paths = glob.glob(os.path.join(CODEX_HOME, "sessions", "*", "*", "*", "rollout-*.jsonl"))
    stamped = []
    for p in paths:
        try:
            stamped.append((os.path.getmtime(p), p))  # resumed sessions append to old files
        except OSError:
            pass
    best = None
    for mtime, path in sorted(stamped, reverse=True)[:max_files]:
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as f:
                f.seek(max(0, size - 512 * 1024))
                lines = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict) or not isinstance(obj.get("payload"), dict):
                continue
            rl = obj["payload"].get("rate_limits")
            if not isinstance(rl, dict) or not (rl.get("primary") or rl.get("secondary")):
                continue
            if rl.get("limit_id") not in (None, limit_id):
                continue  # another model's bucket (e.g. a Spark session) is not the executor's quota
            when = _record_time(obj, mtime)
            if best is None or when > best[0]:
                best = (when, rl)
            break  # newest reading in this file found
    if not best:
        return None
    data = normalize(best[1], "rollout", observed_at=best[0])
    data["limit_id"] = best[1].get("limit_id")
    return data


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_cache(data):
    _ensure_cache_dir()
    tmp = CACHE_FILE + f".{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, CACHE_FILE)


def _live_recently_failed():
    try:
        return time.time() - os.path.getmtime(LIVE_FAIL_MARK) < cache_seconds()
    except OSError:
        return False


def refresh(notify=False):
    """Fetch live (fallback rollout) under a non-blocking lock. None if another refresh runs."""
    _ensure_cache_dir()
    lock = open(LOCK_FILE, "w")
    try:
        if fcntl:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None
        data = fetch_live()
        if data:
            save_cache(data)
            try:
                os.remove(LIVE_FAIL_MARK)
            except OSError:
                pass
        else:
            with open(LIVE_FAIL_MARK, "w"):
                pass
            cached = load_cache()
            roll = fetch_rollout()
            if roll and (not cached or roll["observed_at"] > cached.get("observed_at", 0)):
                save_cache(roll)
                data = roll
            else:
                data = cached or roll  # never replace a newer reading with an older one
        data = apply_resets(data)
        if notify and data:
            maybe_notify(data)
        return data
    finally:
        lock.close()


def spawn_refresh():
    """Start a detached refresh at most once per 30 s (used by the statusline)."""
    if _live_recently_failed():
        return
    try:
        _ensure_cache_dir()
        if time.time() - os.path.getmtime(SPAWN_MARK) < 30:
            return
    except OSError:
        pass
    try:
        with open(SPAWN_MARK, "w"):
            pass
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "--refresh", "--notify", "--quiet"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def get(max_age=None, block=True, notify=False):
    """Cached quota if fresh; otherwise refresh (block) or schedule a refresh (non-block)."""
    max_age = cache_seconds() if max_age is None else max_age
    data = load_cache()
    if not (data and time.time() - data.get("fetched_at", 0) < max_age):
        if block:
            if max_age == 0 or not _live_recently_failed():
                data = refresh(notify=False) or data
            data = data or fetch_rollout()
        else:
            spawn_refresh()
            data = data or fetch_rollout()
    data = apply_resets(data)
    if notify and data:
        maybe_notify(data)
    return data


def low_window_keys(data, threshold=None):
    """Identifiers of the current (not yet reset) windows below the threshold."""
    threshold = warn_threshold() if threshold is None else threshold
    data = apply_resets(data)
    return sorted(f"{w['name']}:{w.get('resets_at')}" for w in (data or {}).get("windows", [])
                  if not w["reset"] and w["remaining"] < threshold)


def already_alerted(key, alerted, tolerance=600):
    """A low window counts as alerted if an alerted key has the same window name and a reset
    time within `tolerance` seconds (rollout/live readings jitter by seconds)."""
    name, _, ts = key.partition(":")
    for k in alerted or []:
        n2, _, t2 = k.partition(":")
        if n2 != name:
            continue
        try:
            if abs(float(ts) - float(t2)) <= tolerance:
                return True
        except ValueError:
            if ts == t2:
                return True
    return False


def is_low(data, threshold=None):
    return bool(low_window_keys(data, threshold))


def fmt_reset(ts, now=None):
    if not ts:
        return "?"
    now = now or time.time()
    left = max(0, int(ts - now))
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    rel = f"{d}d{h}h" if d else (f"{h}h{m}m" if h else f"{m}m")
    return f"{time.strftime('%m-%d %H:%M', time.localtime(ts))}（还有 {rel}）"


def describe(data, threshold=None):
    if not data:
        return "Codex 额度：无法获取（codex 未登录或 app-server 不可用）"
    threshold = warn_threshold() if threshold is None else threshold
    data = apply_resets(data)
    parts = []
    for w in data.get("windows", []):
        if w["reset"]:
            parts.append(f"{w['label']} 窗口已重置，暂无新读数")
        else:
            parts.append(f"{w['label']} 窗口剩余 {w['remaining']:.0f}%，{fmt_reset(w['resets_at'])} 重置")
    cr = data.get("credits") or {}
    if cr.get("unlimited"):
        parts.append("credits 无限")
    elif cr.get("has") and cr.get("balance") not in (None, "", "0"):
        parts.append(f"credits {cr['balance']}")
    if data.get("reset_credits"):
        parts.append(f"可用重置券 {data['reset_credits']} 张")
    if data.get("source") == "rollout":
        age = int((time.time() - data.get("observed_at", time.time())) / 60)
        parts.append(f"数据取自 {age} 分钟前的会话记录")
    line = "Codex 额度：" + "；".join(parts or ["无窗口数据"])
    if is_low(data, threshold):
        line = (f"BATON::QUOTA_LOW 剩余 {data['remaining']:.0f}%，低于 {threshold:g}% 阈值，"
                f"请提醒用户。\n" + line)
    return line


def maybe_notify(data, threshold=None):
    """Desktop notification once per low window (a window is identified by its reset time)."""
    keys = low_window_keys(data, threshold)
    if not keys:
        return False
    if os.environ.get("BATON_DESKTOP_NOTIFY") == "0" \
            or not skill_config().get("quota", {}).get("desktop_notify", True):
        return False
    try:
        with open(NOTIFY_FILE) as f:
            seen = set(json.load(f).get("keys") or [])
    except Exception:
        seen = set()
    if all(already_alerted(k, seen) for k in keys):
        return False
    threshold = warn_threshold() if threshold is None else threshold
    data = apply_resets(data)
    text = f"Codex 额度剩余 {data['remaining']:.0f}%，已低于 {threshold:g}%"
    if data.get("reset_credits"):
        text += f"；可用重置券 {data['reset_credits']} 张"
    if sys.platform == "darwin":
        script = f'display notification "{text}" with title "Baton" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        sys.stderr.write(text + "\n")
    now = time.time()
    # keep only keys whose window has not reset yet
    def current(k):
        try:
            return float(k.split(":", 1)[1]) > now
        except ValueError:
            return True
    live = {k for k in seen | set(keys) if current(k)}
    _ensure_cache_dir()
    with open(NOTIFY_FILE, "w") as f:
        json.dump({"keys": sorted(live), "at": now}, f)
    return True


def main(argv):
    as_json = "--json" in argv
    quiet = "--quiet" in argv
    notify = "--notify" in argv
    max_age = None
    if "--max-age" in argv:
        max_age = int(argv[argv.index("--max-age") + 1])
    if "--refresh" in argv:
        data = refresh(notify=notify) or apply_resets(load_cache())
    else:
        data = get(max_age=max_age, block=True, notify=notify)
    if not quiet:
        print(json.dumps(data, ensure_ascii=False, indent=2) if as_json else describe(data))
    if not data:
        return 2
    return 10 if is_low(data) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
