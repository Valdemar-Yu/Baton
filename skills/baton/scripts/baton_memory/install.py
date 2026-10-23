"""Deployment helpers for ``baton memory install``.

The real install is intentionally explicit and idempotent.  Tests inject fake
commands; no network operation is started at import time.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys
import baton_platform as platform_support
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .ledger import _normalise_baton_home
from .mem0_adapter import BASE_MODEL, MODEL_NAME


PACKAGES = ["mem0ai>=2.0.16,<3", "ollama", "mcp>=1.26,<2"]


def find_python(explicit: Optional[str] = None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if os.name == "nt":
        return sys.executable
    candidates = sorted(glob.glob(os.path.expanduser("~/.local/share/uv/python/cpython-3.11*/bin/python3.11")))
    for value in candidates:
        if os.path.isfile(value) and os.access(value, os.X_OK):
            return value
    raise FileNotFoundError("找不到 uv 的 CPython 3.11，请用 --python 指定")


def _run(command: List[str], runner: Optional[Callable[..., Any]] = None, **kwargs: Any) -> Any:
    call = runner or subprocess.run
    return call(command, check=False, **kwargs)


def _site_packages(python: str) -> str:
    probe = subprocess.run([python, "-c", "import site; print(site.getsitepackages()[0])"], capture_output=True, text=True, check=True)
    return probe.stdout.strip().splitlines()[-1]


def _write_pth(python: str, scripts_dir: str) -> str:
    site = _site_packages(python)
    os.makedirs(site, exist_ok=True)
    path = os.path.join(site, "baton_memory.pth")
    Path(path).write_text(os.path.abspath(scripts_dir) + "\n", encoding="utf-8")
    return path


def _registry_fallback(base_model: str, ollama_dir: Optional[str] = None,
                      curl: str = "curl", runner: Optional[Callable[..., Any]] = None) -> bool:
    """Best-effort fake-ip fallback.

    Ollama's on-disk manifest format changes between releases, so this helper
    verifies downloaded digests and writes blobs into the standard blobs path,
    then lets a normal ``ollama pull`` retry assemble the model metadata.
    """
    root = os.path.expanduser(ollama_dir or os.environ.get("OLLAMA_MODELS", "~/.ollama/models"))
    manifests = os.path.join(root, "manifests", "registry.ollama.ai", "library", base_model.split(":", 1)[0])
    tag = base_model.split(":", 1)[1] if ":" in base_model else "latest"
    url = "https://registry.ollama.ai/v2/library/%s/manifests/%s" % (base_model.split(":", 1)[0], tag)
    temp = tempfile.NamedTemporaryFile(prefix="baton-manifest-", delete=False)
    temp.close()
    try:
        result = _run([curl, "-fsSL", url, "-o", temp.name], runner=runner)
        if getattr(result, "returncode", 1) != 0:
            return False
        value = json.loads(Path(temp.name).read_text(encoding="utf-8"))
        layers = list(value.get("layers", [])) + ([value.get("config")] if value.get("config") else [])
        for layer in layers:
            digest = str(layer.get("digest", ""))
            if not digest.startswith("sha256:"):
                continue
            blob = os.path.join(root, "blobs", digest.replace(":", "-"))
            os.makedirs(os.path.dirname(blob), exist_ok=True)
            url_blob = "https://registry.ollama.ai/v2/library/%s/blobs/%s" % (base_model.split(":", 1)[0], digest)
            result = _run([curl, "-fsSL", url_blob, "-o", blob], runner=runner)
            if getattr(result, "returncode", 1) != 0:
                return False
            digest_actual = hashlib.sha256(Path(blob).read_bytes()).hexdigest()
            if digest_actual != digest.split(":", 1)[1]:
                raise RuntimeError("模型 blob sha256 校验失败：%s" % digest)
        os.makedirs(manifests, exist_ok=True)
        Path(os.path.join(manifests, tag)).write_bytes(Path(temp.name).read_bytes())
        return True
    finally:
        try:
            os.unlink(temp.name)
        except OSError:
            pass


def install_memory(baton_home: Optional[str] = None, python: Optional[str] = None,
                   scripts_dir: Optional[str] = None, runner: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    home = _normalise_baton_home(baton_home if baton_home is not None else None)
    venv = os.path.join(home, "venv")
    py311 = find_python(python)
    scripts_dir = os.path.abspath(scripts_dir or os.path.dirname(os.path.dirname(__file__)))
    os.makedirs(home, exist_ok=True)
    if not os.path.exists(platform_support.venv_python(home)):
        result = _run([py311, "-m", "venv", venv], runner=runner)
        if getattr(result, "returncode", 1) != 0:
            raise RuntimeError("创建记忆 venv 失败")
    vpy = platform_support.venv_python(home)
    pip_result = _run([vpy, "-m", "pip", "install", *PACKAGES], runner=runner, capture_output=True, text=True)
    if getattr(pip_result, "returncode", 1) != 0:
        raise RuntimeError("安装 mem0ai/ollama/mcp 失败")
    pth = _write_pth(vpy, scripts_dir)
    modelfile = os.path.join(home, "Modelfile.baton-qwen3-embed")
    Path(modelfile).write_text("FROM %s\nPARAMETER num_ctx 1024\n" % BASE_MODEL, encoding="utf-8")
    ollama = shutil.which("ollama") or "ollama"
    show = _run([ollama, "show", BASE_MODEL], runner=runner, capture_output=True, text=True)
    pulled = False
    if getattr(show, "returncode", 1) != 0:
        pull = _run([ollama, "pull", BASE_MODEL], runner=runner, capture_output=True, text=True)
        if getattr(pull, "returncode", 1) != 0:
            pulled = _registry_fallback(BASE_MODEL, runner=runner) or pulled
            if not pulled:
                raise RuntimeError("基础嵌入模型不可用，ollama pull 和 registry 退路都失败")
    created = _run([ollama, "create", MODEL_NAME, "-f", modelfile], runner=runner, capture_output=True, text=True)
    if getattr(created, "returncode", 1) != 0:
        raise RuntimeError("创建 %s 失败" % MODEL_NAME)
    return {"baton_home": home, "venv": venv, "python": vpy, "pth": pth,
            "modelfile": modelfile, "model": MODEL_NAME, "pulled": pulled}


__all__ = ["install_memory", "find_python", "PACKAGES"]

