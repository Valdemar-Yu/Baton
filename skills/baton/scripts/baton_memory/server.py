"""Read-only stdio MCP server for Baton memory and archive search."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ledger import LedgerError, MemoryLedger, _normalise_baton_home
from .search import RULE_KINDS, keyword_search


TOOL_DESCRIPTION = "每个事实单独写一条短查询，不要把几件事拼成一条。"


def _find_project(start: Optional[str] = None) -> str:
    configured_root = os.environ.get("BATON_PROJECT_ROOT", "").strip()
    if configured_root and "${" not in configured_root:
        return str(Path(configured_root).expanduser().resolve())
    current = Path(start or os.getcwd()).resolve()
    while True:
        if (current / ".baton").is_dir():
            return str(current)
        if current.parent == current:
            return str(Path(start or os.getcwd()).resolve())
        current = current.parent


def _runtime() -> Tuple[str, str, str, bool, str, str]:
    root = _find_project()
    task = os.environ.get("BATON_TASK", "")
    leg = os.environ.get("BATON_LEG", "")
    started = os.environ.get("BATON_LEG_STARTED", "")
    raw_home = os.environ.get("BATON_HOME", "")
    configured_home = _normalise_baton_home(raw_home or None)
    home_note = ""
    if not raw_home or "${" in raw_home or not os.path.isabs(os.path.expanduser(raw_home)):
        if "${" in raw_home:
            home_note = "BATON_HOME 未展开，使用默认 ~/.baton"
        elif raw_home and not os.path.isabs(os.path.expanduser(raw_home)):
            home_note = "BATON_HOME 不是绝对路径，使用默认 ~/.baton"
    expanded = bool(task and leg and started and not any("${" in value for value in (task, leg, started)))
    return root, task, leg, expanded, configured_home, home_note


def _call_path(root: str, task: str, leg: str) -> str:
    return os.path.join(root, ".baton", "runs", task, "leg-%03d.memory-calls.jsonl" % int(leg))


def _record_calls(root: str, task: str, leg: str, records: Iterable[str]) -> None:
    started = os.environ.get("BATON_LEG_STARTED", "")
    if not task or not leg or not started or any("${" in value for value in (task, leg, started)):
        return
    path = _call_path(root, task, leg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        for ident in records:
            stream.write(json.dumps({"time": time.time(), "ledger_id": ident}, ensure_ascii=False) + "\n")


def _alert(root: str, task: str, leg: str, error: str) -> None:
    started = os.environ.get("BATON_LEG_STARTED", "")
    if not task or not leg or not started or any("${" in value for value in (task, leg, started)):
        return
    path = os.path.join(root, ".baton", "runs", task, "leg-%03d.alert" % int(leg))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text("记忆台账校验失败：%s\n" % error, encoding="utf-8")


def _ready_marker(root: str, task: str, leg: str) -> None:
    started = os.environ.get("BATON_LEG_STARTED", "")
    if not task or not leg or not started or any("${" in value for value in (task, leg, started)):
        return
    path = os.path.join(root, ".baton", "runs", task, "leg-%03d.mcp-ready" % int(leg))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text(json.dumps({"time": time.time(), "pid": os.getpid()}, ensure_ascii=False) + "\n", encoding="utf-8")


def _ledger_or_alert() -> Tuple[MemoryLedger, str, str, bool]:
    root, task, leg, expanded, configured_home, home_note = _runtime()
    ledger = MemoryLedger(root, baton_home=configured_home or None)
    good, error = ledger.verify(repair=True)
    if not good:
        _alert(root, task, leg, error)
        raise LedgerError(error)
    return ledger, root, task, expanded, home_note


def _all_latest(ledger: MemoryLedger) -> Dict[str, Dict[str, Any]]:
    return ledger.latest()


def _superseded(query: str, ledger: MemoryLedger, limit: int = 10) -> List[Dict[str, Any]]:
    # Keyword route is deterministic and also makes the server useful while
    # Ollama is offline.  Active entries are always emitted first.
    candidates = keyword_search(ledger, query, top_k=50)
    latest = _all_latest(ledger)
    active = [item for item in candidates if latest.get(item.get("ledger_id"), {}).get("status", "active") == "active"]
    active_ids = {item.get("ledger_id") for item in active}
    old: List[Dict[str, Any]] = []
    for ident, item in latest.items():
        if item.get("status") != "superseded" or ident in active_ids:
            continue
        hay = str(item.get("text", "")).lower()
        if any(term in hay for term in re.findall(r"\w+|[\u3400-\u9fff]", query.lower())):
            value = dict(item)
            value["ledger_id"] = ident
            value["text"] = "%s（已被 %s 取代）" % (value.get("text", ""), value.get("replaced_by", "未知条目"))
            old.append(value)
    return (active + old)[:limit]


def _memory_search_impl(queries: List[str]) -> Dict[str, Any]:
    ledger, root, task, expanded, home_note = _ledger_or_alert()
    if not isinstance(queries, list):
        raise TypeError("queries 必须是字符串数组")
    items: List[Dict[str, Any]] = []
    seen = set()
    semantic = None
    fallback_reason = ""
    try:
        from .mem0_adapter import Mem0Index
        index = Mem0Index(ledger)
        class _Semantic:
            def search(self, query, top_k=30, filters=None):
                return index.search(query, top_k=top_k, filters=filters)
        semantic = _Semantic()
    except Exception as exc:
        from .mem0_adapter import is_expected_fallback_error
        if not is_expected_fallback_error(exc):
            raise
        fallback_reason = str(exc)
    for query in queries:
        if not isinstance(query, str):
            continue
        active = []
        if semantic is not None:
            from .search import retrieve_memory
            value = retrieve_memory(ledger, [query], semantic=semantic, top_k=10)
            fallback_reason = fallback_reason or str(value.get("fallback_reason", ""))
            active.extend(value.get("results", []))
        archived = _superseded(query, ledger, limit=10)
        if not active:
            active.extend(archived)
        else:
            active_ids = {item.get("ledger_id") for item in active}
            active.extend(item for item in archived if item.get("ledger_id") not in active_ids)
        for item in active[:10]:
            ident = item.get("ledger_id")
            if ident in seen:
                continue
            seen.add(ident)
            items.append(item)
    _record_calls(root, task, os.environ.get("BATON_LEG", ""), [str(x) for x in seen])
    value: Dict[str, Any] = {"results": items, "tool": "memory_search"}
    notes = [note for note in (home_note, "语义检索退回关键词：" + fallback_reason if fallback_reason else "") if note]
    if notes:
        value["note"] = "；".join(notes)
    if not expanded:
        value["note"] = (value.get("note", "") + "；" if value.get("note") else "") + "BATON_TASK/BATON_LEG 未展开，只检索、不写 memory-calls 记录"
    return value


def archive_search_impl(query: str, root: Optional[str] = None) -> List[Dict[str, Any]]:
    root = root or _find_project()
    base = Path(root) / ".baton"
    if not base.is_dir():
        return []
    patterns = ["log.md", "reports/*.html", "reviews/*", "decisions/*", "tasks/*/brief.md",
                "runs/*/leg-*.verify.json", "memory/ledger.export.md"]
    files: List[Path] = []
    for pattern in patterns:
        files.extend(base.glob(pattern))
    needle = str(query or "").lower()
    result: List[Dict[str, Any]] = []
    for path in sorted(set(files)):
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, 1):
            if needle not in line.lower():
                continue
            lo, hi = max(0, index - 2), min(len(lines), index + 1)
            result.append({"file": str(path.relative_to(base)), "line": index,
                           "context": "\n".join(lines[lo:hi])})
    return result


def _archive_search_impl(query: str) -> Dict[str, Any]:
    return {"results": archive_search_impl(query), "tool": "archive_search"}


def create_server() -> Any:
    from mcp.server.fastmcp import FastMCP  # type: ignore
    app = FastMCP("baton-memory")

    @app.tool(description=TOOL_DESCRIPTION)
    def memory_search(queries: List[str]) -> Dict[str, Any]:
        return _memory_search_impl(queries)

    @app.tool(description="在 Baton .baton 原始档案中全文检索，返回文件名、行号和上下文。")
    def archive_search(query: str) -> Dict[str, Any]:
        return _archive_search_impl(query)

    return app


def main() -> None:
    root, task, leg, expanded, _, _ = _runtime()
    if expanded and os.environ.get("BATON_MCP_PREFLIGHT") != "1":
        _ready_marker(root, task, leg)
    app = create_server()
    app.run(transport="stdio")


if __name__ == "__main__":
    main()

