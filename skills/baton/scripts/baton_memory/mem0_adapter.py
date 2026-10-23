"""Small, defensive mem0/Ollama adapter.

All third-party imports are lazy.  The adapter is therefore usable with the
stdlib and a fake ``mem0`` module in the repository tests.
"""

from __future__ import annotations

import json
import os
import baton_platform as platform_support
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional

from .ledger import MemoryLedger


MODEL_NAME = "baton-qwen3-embed:latest"
BASE_MODEL = "qwen3-embedding:0.6b"
MODEL_DIMS = 1024
OLLAMA_URL = "http://127.0.0.1:11434"
COLLECTION = "baton_qwen3emb06b_1024"


def _set_mem0_env(ledger: MemoryLedger) -> None:
    # This must happen before ``from mem0 import Memory`` in every process.
    os.environ["MEM0_TELEMETRY"] = "false"
    os.environ["MEM0_DIR"] = ledger.memory_dir


def ollama_base_url() -> str:
    return os.environ.get("BATON_OLLAMA_URL", OLLAMA_URL)


def ollama_tags(timeout: float = 2.0) -> List[str]:
    request = urllib.request.Request(ollama_base_url().rstrip("/") + "/api/tags", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                value = json.loads(response.read().decode("utf-8"))
            finally:
                response.close()
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise RuntimeError("Ollama 不在线：%s" % exc) from exc
    models = value.get("models", []) if isinstance(value, dict) else []
    return [str(x.get("name")) for x in models if isinstance(x, dict) and x.get("name")]


def ensure_model(timeout: float = 2.0) -> None:
    names = ollama_tags(timeout=timeout)
    if MODEL_NAME not in names:
        raise RuntimeError("Ollama 缺少模型 %s" % MODEL_NAME)


def mem0_config(ledger: MemoryLedger, collection: str = COLLECTION, dims: int = MODEL_DIMS) -> Dict[str, Any]:
    _set_mem0_env(ledger)
    return {
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "collection_name": collection,
                "embedding_model_dims": dims,
                "path": os.path.join(ledger.memory_dir, "qdrant"),
            },
        },
        "embedder": {
            "provider": "ollama",
            "config": {
                "model": MODEL_NAME,
                "ollama_base_url": ollama_base_url(),
            },
        },
        "llm": {"provider": "ollama", "config": {"model": "llama3.2:latest", "ollama_base_url": ollama_base_url()}},
        "history_db_path": os.path.join(ledger.memory_dir, "history.db"),
    }


def build_mem0(ledger: MemoryLedger, collection: str = COLLECTION, dims: int = MODEL_DIMS) -> Any:
    ensure_model(timeout=2.0)
    warm_ollama(timeout=10.0)
    _set_mem0_env(ledger)
    from mem0 import Memory  # type: ignore
    config = mem0_config(ledger, collection=collection, dims=dims)
    if hasattr(Memory, "from_config"):
        return Memory.from_config(config)
    return Memory(config)


@contextmanager
def _project_index_lock(ledger: MemoryLedger, timeout: float = 5.0):
    path = os.path.join(ledger.memory_dir, "index.lock")
    os.makedirs(ledger.memory_dir, exist_ok=True)
    handle = open(path, "a+b")
    try:
        import fcntl
    except ImportError:  # pragma: no cover - non-POSIX
        fcntl = None
    if fcntl is None:
        try:
            deadline = time.monotonic() + timeout
            while not platform_support.lock_file(handle, blocking=False):
                if time.monotonic() >= deadline:
                    raise TimeoutError("mem0 project index lock timed out")
                time.sleep(0.02)
            yield handle
        finally:
            handle.close()
        return
    try:
        deadline = __import__("time").monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if __import__("time").monotonic() >= deadline:
                    raise TimeoutError("mem0 项目锁等待超过 5 秒")
                __import__("time").sleep(0.02)
        yield handle
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _close_client(value: Any) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class Mem0Index:
    """One-shot mem0 index handle; every operation closes Qdrant in finally."""

    def __init__(self, ledger: MemoryLedger, collection: str = COLLECTION, timeout: float = 5.0,
                 dims: int = MODEL_DIMS):
        self.ledger = ledger
        self.collection = collection
        self.timeout = timeout
        self.dims = dims

    def _call(self, callback: Any) -> Any:
        with _project_index_lock(self.ledger, timeout=self.timeout):
            memory = build_mem0(self.ledger, collection=self.collection, dims=self.dims)
            try:
                return callback(memory)
            finally:
                vector_store = getattr(memory, "vector_store", None)
                client = getattr(vector_store, "client", None)
                close = getattr(client, "close", None)
                if callable(close):
                    close()
                _close_client(getattr(getattr(memory, "embedding_model", None), "client", None))
                _close_client(getattr(getattr(memory, "llm", None), "client", None))
                close_memory = getattr(memory, "close", None)
                if callable(close_memory):
                    close_memory()

    def add(self, record: Dict[str, Any]) -> Any:
        return self._call(lambda m: m.add(str(record["text"]), agent_id="baton",
                                           metadata={"ledger_id": record["id"], "kind": record["kind"]}, infer=False))

    def add_many(self, records: Iterable[Dict[str, Any]]) -> int:
        count = 0
        for record in records:
            self.add(record)
            count += 1
        return count

    def search(self, query: str, top_k: int = 30, filters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        def call(memory: Any) -> Any:
            return memory.search(query, top_k=top_k, filters=filters or {"agent_id": "baton"})
        value = self._call(call)
        if isinstance(value, dict):
            return list(value.get("results", value.get("memories", [])) or [])
        return list(value or [])

    def reindex(self, all_records: bool = False) -> int:
        latest = self.ledger.latest()
        records = [value for value in latest.values()
                   if value.get("status") == "active"]
        if not all_records:
            records = [value for value in records if value.get("index_pending")]
        # Ledger IDs are immutable; this also makes reindex safe with a fake or
        # real mem0 implementation that does not expose a delete API.
        target = self
        if all_records:
            target = Mem0Index(self.ledger, collection="%s_rebuild_%d" % (self.collection, int(time.time())),
                               timeout=self.timeout, dims=self.dims)
        count = 0
        for record in records:
            target.add(record)
            self.ledger.append_index_clear(record["id"])
            count += 1
        return count


def is_expected_fallback_error(exc: BaseException) -> bool:
    """Only availability/model/timeout failures may take the keyword path."""
    if isinstance(exc, (TimeoutError, ConnectionError, urllib.error.URLError, ImportError)):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in (
        "ollama 不在线", "ollama offline", "connection refused", "timed out",
        "timeout", "缺少模型", "model not found", "model.*not found", "超过 10 秒",
    ))


def warm_ollama(timeout: float = 10.0) -> None:
    """Prewarm Ollama with the keep-alive option unsupported by mem0's config."""
    result: Dict[str, Any] = {}
    error: Dict[str, BaseException] = {}

    def worker() -> None:
        try:
            import ollama  # type: ignore
            client_cls = getattr(ollama, "Client", None)
            client = client_cls(host=ollama_base_url()) if client_cls else ollama
            try:
                client.embed(model=MODEL_NAME, input="Baton memory warmup", options={"keep_alive": "10m"})
            finally:
                if client_cls:
                    _close_client(client)
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("Ollama 嵌入超过 10 秒")
    if "value" in error:
        raise error["value"]


def embed_text(text: str, timeout: float = 10.0) -> List[float]:
    """Embed through ollama in a bounded worker, preserving dimension errors."""
    result: Dict[str, Any] = {}
    error: Dict[str, BaseException] = {}

    def worker() -> None:
        try:
            import ollama  # type: ignore
            client_cls = getattr(ollama, "Client", None)
            client = client_cls(host=ollama_base_url()) if client_cls else ollama
            try:
                value = client.embed(model=MODEL_NAME, input=text, options={"keep_alive": "10m"})
            finally:
                if client_cls:
                    _close_client(client)
            embeddings = value.get("embeddings", []) if isinstance(value, dict) else getattr(value, "embeddings", [])
            result["value"] = embeddings[0] if embeddings else []
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError("Ollama 嵌入超过 10 秒")
    if "value" in error:
        raise error["value"]
    value = result.get("value")
    if not isinstance(value, list):
        raise RuntimeError("Ollama 返回没有 embeddings")
    return value


__all__ = ["Mem0Index", "MODEL_NAME", "MODEL_DIMS", "COLLECTION", "mem0_config", "build_mem0", "embed_text", "ensure_model", "warm_ollama", "is_expected_fallback_error"]

