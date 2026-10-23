"""Parsing and approval helpers for v2 memory candidates.

The report template carries candidates in one small JSON block.  Keeping the
parser independent from ``baton.py`` makes the report format testable on the
host Python 3.9 and gives the dossier/rule paths one normalised representation.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .ledger import MemoryLedger
from .search import retrieve_memory


MEMORY_KINDS = {"command", "convention", "pitfall", "precedent", "map", "event"}
_HEADING_RE = re.compile(r"(?is)(?:<h2[^>]*>\s*(?:记忆候选|Memory candidates)\s*</h2>|^\s*##\s+(?:记忆候选|Memory candidates)\s*$)")
_NEXT_HEADING_RE = re.compile(r"(?im)(?:<h[1-2][^>]*>|^\s*#{1,2}\s+)")


class CandidateError(ValueError):
    pass


def _section(text: str) -> str:
    match = _HEADING_RE.search(text or "")
    if not match:
        return ""
    tail = text[match.end():]
    nxt = _NEXT_HEADING_RE.search(tail)
    return tail[:nxt.start()] if nxt else tail


def _decode_block(raw: str) -> Any:
    raw = raw.strip()
    # A report may use either a fenced block or a ``pre`` block.  Decode the
    # first complete JSON array/object so explanatory prose around it is safe.
    for pattern in (
        r"```(?:json|memory-candidates)\s*\n(.*?)\n```",
        r"<pre[^>]*data-memory-candidates[^>]*>(.*?)</pre>",
        r"<script[^>]*type=[\"']application/json[\"'][^>]*data-memory-candidates[^>]*>(.*?)</script>",
    ):
        match = re.search(pattern, raw, re.I | re.S)
        if match:
            raw = match.group(1).strip()
            if pattern.startswith("<pre"):
                # Text inside <pre> is HTML: &quot; and &amp; are escaped characters.
                raw = html.unescape(raw)
            break
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Permit a JSON array embedded in a pre block containing HTML escaped
        # text only when it is otherwise unambiguous.
        start = min([x for x in (raw.find("["), raw.find("{")) if x >= 0] or [-1])
        end = max(raw.rfind("]"), raw.rfind("}"))
        if start >= 0 and end >= start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        raise CandidateError("记忆候选 JSON 无法解析")


def _normalise(value: Any, index: int) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateError("第 %d 条记忆候选必须是 object" % index)
    kind = value.get("kind", value.get("type"))
    if kind not in MEMORY_KINDS:
        raise CandidateError("第 %d 条记忆候选 kind 无效：%r" % (index, kind))
    text = value.get("text", value.get("body"))
    if not isinstance(text, str) or not text.strip():
        raise CandidateError("第 %d 条记忆候选 text 不能为空" % index)
    source = value.get("source", "executor")
    if not isinstance(source, str) or not source.strip():
        raise CandidateError("第 %d 条记忆候选 source 不能为空" % index)
    anchors = value.get("anchors", [])
    if isinstance(anchors, str):
        anchors = [anchors]
    if not isinstance(anchors, list) or not 1 <= len(anchors) <= 3 or any(not isinstance(x, str) or not x.strip() for x in anchors):
        raise CandidateError("第 %d 条记忆候选 anchors 必须有 1 到 3 个字符串" % index)
    out = {"kind": kind, "text": text, "source": source,
           "anchors": [str(x) for x in anchors]}
    for key in ("reason", "actor", "verified"):
        if key in value:
            out[key] = value[key]
    return out


def parse_memory_candidates(report: str) -> List[Dict[str, Any]]:
    """Parse the stable candidate block in an executor report.

    Missing sections are valid and mean that the executor proposed no memory.
    Malformed blocks fail the dossier rather than silently dropping facts.
    """
    section = _section(report or "")
    if not section:
        return []
    # Prefer the explicit data attribute, then fenced JSON.  A bare JSON array
    # is accepted for hand-written reports as a compatibility fallback.
    value = _decode_block(section)
    if isinstance(value, dict):
        value = value.get("candidates")
    if not isinstance(value, list):
        raise CandidateError("记忆候选必须是 JSON 数组")
    return [_normalise(item, i) for i, item in enumerate(value, 1)]


def number_candidates(candidates: Iterable[Dict[str, Any]], prefix: str = "C") -> List[Dict[str, Any]]:
    result = []
    for i, item in enumerate(candidates, 1):
        value = dict(item)
        value["id"] = "%s%d" % (prefix, i)
        result.append(value)
    return result


def nearest_candidates(ledger: MemoryLedger, candidate: Dict[str, Any], top_k: int = 3,
                       semantic: Any = None) -> List[Dict[str, Any]]:
    """Return the closest active entries using the same two-route retriever."""
    value = retrieve_memory(ledger, [candidate.get("text", "")], semantic=semantic, top_k=max(3, top_k))
    return list(value.get("results", []))[:top_k]


def candidate_actor(candidate: Dict[str, Any]) -> str:
    source = str(candidate.get("source", "executor")).lower()
    if "conductor" in source or "指挥" in source:
        return "conductor"
    if "monitor" in source or "监督" in source:
        return "monitor"
    if "user" in source or "用户" in source:
        return "user"
    return "executor"


__all__ = ["CandidateError", "MEMORY_KINDS", "parse_memory_candidates", "number_candidates",
           "nearest_candidates", "candidate_actor"]
