"""Append-only project memory ledger with a recoverable hash chain.

This module only uses the Python standard library.  It is intentionally usable
on the host Python 3.9 during tests; the installed venv uses the same code.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import baton_platform as platform_support
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


KINDS = {"command", "convention", "pitfall", "precedent", "map", "event"}
STATUSES = {"active", "superseded", "retired", "suspended"}
ID_RE = re.compile(r"^M\d{3,}$")


def _now() -> str:
    return _datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _json_bytes(value: Dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash_record(record: Dict[str, Any]) -> str:
    body = dict(record)
    body.pop("record_hash", None)
    return hashlib.sha256(_json_bytes(body)).hexdigest()


def _project_id(project: Any) -> str:
    if hasattr(project, "project_id"):
        return str(project.project_id)
    root = os.path.realpath(os.fspath(project))
    return "%s-%s" % (os.path.basename(root), hashlib.sha1(os.fsencode(root)).hexdigest()[:8])


def _project_root(project: Any) -> str:
    return os.path.realpath(os.fspath(getattr(project, "root", project)))


def _normalise_baton_home(value: Optional[str] = None) -> str:
    """Return a safe absolute BATON_HOME; shell placeholders never become paths."""
    raw = os.environ.get("BATON_HOME") if value is None else value
    if raw:
        raw = os.fspath(raw)
    if not raw or "${" in raw:
        raw = "~/.baton"
    expanded = os.path.expanduser(str(raw))
    if not os.path.isabs(expanded):
        expanded = os.path.expanduser("~/.baton")
    return os.path.abspath(expanded)


def _baton_home() -> str:
    return _normalise_baton_home()


class LedgerError(RuntimeError):
    """Raised when a ledger cannot be safely read or written."""


class LedgerCorrupt(LedgerError):
    def __init__(self, message: str, index: Optional[int] = None):
        super().__init__(message)
        self.index = index


@contextmanager
def _file_lock(path: str, exclusive: bool) -> Iterator[Any]:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with (platform_support.exclusive_lock(path) if os.name == "nt" else open(path, "a+b")) as handle:
        if fcntl is not None:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), mode)
        try:
            yield handle
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class MemoryLedger:
    """One project's append-only memory ledger."""

    def __init__(self, project: Any, baton_home: Optional[str] = None):
        self.project = project
        self.project_root = _project_root(project)
        self.project_id = _project_id(project)
        home = _normalise_baton_home(baton_home) if baton_home is not None else _baton_home()
        self.memory_dir = os.path.join(home, "projects", self.project_id, "memory")
        self.ledger_path = os.path.join(self.memory_dir, "ledger.jsonl")
        self.head_path = os.path.join(self.memory_dir, "head")
        self.lock_path = os.path.join(self.memory_dir, "ledger.lock")
        self.export_path = os.path.join(self.project_root, ".baton", "memory", "ledger.export.md")

    def _head(self) -> str:
        try:
            return Path(self.head_path).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _raw_lines(self) -> Tuple[List[bytes], bool]:
        try:
            data = Path(self.ledger_path).read_bytes()
        except FileNotFoundError:
            return [], True
        return data.splitlines(keepends=True), not data or data.endswith(b"\n")

    def _records_unlocked(self) -> List[Dict[str, Any]]:
        lines, newline = self._raw_lines()
        records: List[Dict[str, Any]] = []
        for n, raw in enumerate(lines, 1):
            if not raw.endswith(b"\n"):
                raise LedgerCorrupt("末行不完整（第 %d 条）" % n, n)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerCorrupt("JSON 损坏（第 %d 条）：%s" % (n, exc), n) from exc
            if not isinstance(value, dict):
                raise LedgerCorrupt("记录不是 object（第 %d 条）" % n, n)
            records.append(value)
        return records

    def records(self, repair: bool = True) -> List[Dict[str, Any]]:
        """Return records while holding a shared ledger lock.

        ``repair`` performs the two crash-only recoveries described by v2: a
        stale chain head is advanced, and a partial final line after that head is
        truncated.  A malformed complete record is never repaired silently.
        """
        with _file_lock(self.lock_path, exclusive=repair):
            return self._records_locked(repair=repair)

    def _records_locked(self, repair: bool) -> List[Dict[str, Any]]:
        lines, complete = self._raw_lines()
        head = self._head()
        records: List[Dict[str, Any]] = []
        partial = bool(lines and not lines[-1].endswith(b"\n"))
        usable = lines[:-1] if partial else lines
        for n, raw in enumerate(usable, 1):
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerCorrupt("JSON 损坏（第 %d 条）：%s" % (n, exc), n) from exc
            if not isinstance(value, dict):
                raise LedgerCorrupt("记录不是 object（第 %d 条）" % n, n)
            records.append(value)
        # If the complete prefix validates through the archived head, a crash
        # between append and head update can safely discard the partial tail.
        if partial and records and self._validate(records, head, allow_head_prefix=True)[0]:
            if repair:
                with open(self.ledger_path, "wb") as stream:
                    stream.write(b"".join(usable))
                    stream.flush()
                    os.fsync(stream.fileno())
            complete = True
        elif partial:
            raise LedgerCorrupt("末行不完整且不在存档链头之后", len(records) + 1)
        ok, error = self._validate(records, head, allow_head_prefix=True)
        if not ok:
            raise LedgerCorrupt(error or "哈希链损坏")
        if records:
            last = records[-1].get("record_hash", "")
            if head != last and repair:
                self._write_head(last)
        elif head and repair:
            raise LedgerCorrupt("空台账却存在链头")
        return records

    @staticmethod
    def _validate(records: Sequence[Dict[str, Any]], head: str, allow_head_prefix: bool = False) -> Tuple[bool, str]:
        if records and not head:
            return False, "台账非空但 memory/head 缺失"
        previous = ""
        head_seen = not head
        for n, record in enumerate(records, 1):
            if record.get("previous_hash", "") != previous:
                return False, "第 %d 条 previous_hash 不匹配" % n
            expected = _hash_record(record)
            if record.get("record_hash") != expected:
                return False, "第 %d 条 record_hash 不匹配" % n
            if record.get("seq") != n:
                return False, "第 %d 条 seq 不匹配" % n
            previous = expected
            if expected == head:
                head_seen = True
        if head and not head_seen:
            return False, "链头不在链上"
        if head and not allow_head_prefix and previous != head:
            return False, "链头不是末条记录"
        return True, ""

    def _write_head(self, value: str) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="head.", dir=self.memory_dir, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(value + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            platform_support.atomic_replace(tmp, self.head_path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def _append_unlocked(self, record: Dict[str, Any]) -> Dict[str, Any]:
        records = self._records_locked(repair=True)
        prior = records[-1].get("record_hash", "") if records else ""
        out = dict(record)
        if out.get("record_type") not in {"status", "index"} and not out.get("id"):
            used = [int(str(item.get("id", "M000"))[1:]) for item in records
                    if ID_RE.match(str(item.get("id", "")))]
            out["id"] = "M%03d" % (max(used or [0]) + 1)
        out["seq"] = len(records) + 1
        out["previous_hash"] = prior
        out["record_hash"] = _hash_record(out)
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.ledger_path, "ab") as stream:
            stream.write(_json_bytes(out) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._write_head(out["record_hash"])
        return out

    def append(self, entries: Iterable[Dict[str, Any]], source: Any = None) -> List[Dict[str, Any]]:
        """Append one or more approved entries and regenerate the export."""
        prepared = [_normalise_entry(item, source=source) for item in entries]
        if not prepared:
            return []
        with _file_lock(self.lock_path, exclusive=True):
            written = [self._append_unlocked(item) for item in prepared]
        self.export()
        return written

    def append_status(self, ledger_id: str, status: str, **extra: Any) -> Dict[str, Any]:
        if status not in STATUSES:
            raise ValueError("unknown memory status: %s" % status)
        with _file_lock(self.lock_path, exclusive=True):
            value = self._append_unlocked({"id": ledger_id, "record_type": "status", "status": status, **extra})
        self.export()
        return value

    def append_index_pending(self, ledger_id: str, error: str = "") -> Dict[str, Any]:
        with _file_lock(self.lock_path, exclusive=True):
            value = self._append_unlocked({"id": ledger_id, "record_type": "index",
                                           "index_pending": True, "index_error": str(error)[:1000]})
        self.export()
        return value

    def append_index_clear(self, ledger_id: str) -> Dict[str, Any]:
        with _file_lock(self.lock_path, exclusive=True):
            value = self._append_unlocked({"id": ledger_id, "record_type": "index",
                                           "index_pending": False})
        self.export()
        return value

    def latest(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for record in self.records():
            ident = record.get("id")
            if isinstance(ident, str) and ID_RE.match(ident):
                record_type = record.get("record_type")
                if record_type == "status":
                    if ident in result:
                        result[ident] = dict(result[ident], status=record.get("status", result[ident].get("status", "active")))
                        result[ident].update({key: value for key, value in record.items()
                                              if key in {"replaced_by", "status_reason", "time", "pinned", "pin_seq", "curation_id"}})
                elif record_type == "index":
                    if ident in result:
                        pending = bool(record.get("index_pending", False))
                        result[ident] = dict(result[ident], index_pending=pending, indexed=not pending)
                        if record.get("index_error"):
                            result[ident]["index_error"] = record["index_error"]
                else:
                    result[ident] = dict(record)
        return result

    def verify(self, repair: bool = True) -> Tuple[bool, str]:
        try:
            self.records(repair=repair)
        except LedgerCorrupt as exc:
            return False, str(exc)
        return True, ""

    def reanchor(self, confirmation: Optional[Any] = None) -> str:
        records = self.records(repair=False)
        if not records:
            raise LedgerError("台账为空，不能重设链头")
        task = getattr(confirmation, "task", None) if confirmation is not None else None
        if callable(confirmation):
            if not confirmation(task or ""):
                raise LedgerError("未确认任务名")
        elif confirmation is False:
            raise LedgerError("未确认任务名")
        with _file_lock(self.lock_path, exclusive=True):
            records = self._records_locked(repair=False)
            self._write_head(records[-1]["record_hash"])
            return records[-1]["record_hash"]

    def export(self) -> str:
        latest = self.latest()
        order = sorted(latest.values(), key=lambda x: (str(x.get("id", "")), int(x.get("seq", 0))))
        lines = ["# Baton memory ledger export", "", "<!-- generated; ledger.jsonl is authoritative -->", ""]
        for record in order:
            if record.get("record_type") == "status":
                continue
            ident = record.get("id", "?")
            kind = record.get("kind", "?")
            source = record.get("source", "")
            status = record.get("status", "active")
            pinned = " pinned" if record.get("pinned") else ""
            lines.append("## %s · %s · %s · %s%s" % (ident, kind, source, status, pinned))
            lines.append(str(record.get("text", record.get("body", ""))))
            lines.append("")
        os.makedirs(os.path.dirname(self.export_path), exist_ok=True)
        tmp = self.export_path + ".tmp.%s" % os.getpid()
        Path(tmp).write_text("\n".join(lines), encoding="utf-8")
        platform_support.replace_readonly(tmp, self.export_path)
        try:
            os.chmod(self.export_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
        return self.export_path


def _normalise_entry(value: Dict[str, Any], source: Any = None) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("memory entry must be an object")
    out = dict(value)
    kind = out.get("kind")
    if kind not in KINDS:
        raise ValueError("memory kind must be one of %s" % ", ".join(sorted(KINDS)))
    text = out.get("text", out.get("body"))
    if not isinstance(text, str) or not text.strip():
        raise ValueError("memory text must be a non-empty string")
    out["text"] = text
    out.pop("body", None)
    out["source"] = str(out.get("source", source or ""))
    out["task"] = str(out.get("task", ""))
    out["leg"] = out.get("leg")
    out["end_snapshot"] = str(out.get("end_snapshot", out.get("snapshot", "")))
    anchors = out.get("anchors", [])
    if isinstance(anchors, str):
        anchors = [anchors]
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 3:
        raise ValueError("memory anchors must contain 1 to 3 paths/identifiers")
    out["anchors"] = [str(x) for x in anchors]
    out["time"] = str(out.get("time") or _now())
    out["status"] = out.get("status", "active")
    if out["status"] not in STATUSES:
        raise ValueError("unknown memory status: %s" % out["status"])
    out["pinned"] = bool(out.get("pinned", False))
    out.pop("id", None)
    out.pop("seq", None)
    out.pop("previous_hash", None)
    out.pop("record_hash", None)
    return out


def memory_append_approved(project: Any, entries: Iterable[Dict[str, Any]], source: Any = None,
                           mem0_writer: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Append approved entries, then index each entry in optional mem0.

    Ledger acknowledgement happens before indexing.  A failed index is recorded
    as an independent index marker; it never changes the lifecycle status and
    never hides the original error from dimension-related failures.
    """
    ledger = project if isinstance(project, MemoryLedger) else MemoryLedger(project)
    written = ledger.append(entries, source=source)
    writer = mem0_writer
    if writer is None:
        try:
            from .mem0_adapter import Mem0Index
            writer = Mem0Index(ledger)
        except Exception:
            writer = None
    if writer is not None:
        for record in written:
            try:
                writer.add(record)
            except Exception as exc:  # index is rebuildable; ledger remains authoritative
                text = str(exc)
                from .mem0_adapter import is_expected_fallback_error
                if not is_expected_fallback_error(exc):
                    raise
                ledger.append_index_pending(record["id"], error=text)
    return written


__all__ = ["MemoryLedger", "LedgerError", "LedgerCorrupt", "memory_append_approved", "KINDS", "STATUSES"]

