"""Tier 1 classifiers (CORE-4): cheap signals that decide what to escalate, never final decisions (D-011).

- LocalClassifier: protectai/deberta-v3-base-prompt-injection-v2 (Apache-2.0), ONNX on CPU, chunked.
- RemoteGuard: Llama Prompt Guard 2 via the router role `tier1_remote` (hosted, free tier).
Both cache by content hash (CC2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path

from .config import repo_root

log = logging.getLogger("intentseal.tier1")
MODEL_REPO = os.environ.get("INTENTSEAL_TIER1_MODEL", "protectai/deberta-v3-base-prompt-injection-v2")


class LocalClassifier:
    def __init__(self, repo: str = MODEL_REPO, threads: int | None = None) -> None:
        import numpy as np  # noqa: F401  (import check)
        import onnxruntime as ort
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        os.environ.setdefault("HF_HOME", str(repo_root() / "data" / "hf_cache"))
        path = Path(snapshot_download(repo, allow_patterns=["onnx/*", "*.json"]))
        tok = path / "onnx" / "tokenizer.json"
        self.tok = Tokenizer.from_file(str(tok if tok.exists() else path / "tokenizer.json"))
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or int(os.environ.get("INTENTSEAL_TIER1_THREADS") or "2")
        self.sess = ort.InferenceSession(str(path / "onnx" / "model.onnx"), so, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        cfg = json.loads((path / "config.json").read_text())
        self.pos = next(int(k) for k, v in cfg["id2label"].items() if "INJ" in v.upper())
        self.cls, self.sep = self.tok.token_to_id("[CLS]"), self.tok.token_to_id("[SEP]")
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, float] = OrderedDict()   # bounded: CACHE_MAX entries

    def score(self, text: str) -> float:
        """Max probability over windows of up to 256 tokens (shorter windows keep CPU latency low)."""
        import numpy as np

        key = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if key in self._cache:
            return self._cache[key]
        ids = self.tok.encode(text, add_special_tokens=False).ids
        best, window, stride = 0.0, 254, 200
        with self._lock:
            for start in range(0, max(1, len(ids)), stride):
                chunk = [self.cls] + ids[start:start + window] + [self.sep]
                feed = {"input_ids": np.array([chunk], dtype=np.int64),
                        "attention_mask": np.ones((1, len(chunk)), dtype=np.int64)}
                if "token_type_ids" in self.inputs:
                    feed["token_type_ids"] = np.zeros((1, len(chunk)), dtype=np.int64)
                logits = self.sess.run(None, feed)[0][0]
                p = np.exp(logits - logits.max())
                best = max(best, float((p / p.sum())[self.pos]))
                if start + window >= len(ids):
                    break
        self._cache[key] = best
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)
        return best


CACHE_MAX = 20000
RETRY_S = 60.0
_loaded: LocalClassifier | None = None
_failure: tuple[float, str] | None = None     # (time, error type) of the last failed load
_load_lock = threading.Lock()


def local_classifier() -> LocalClassifier | None:
    """The process-wide local classifier. A failed load is retried after RETRY_S (never cached for the life of the
    process); while it is unavailable the engine sends content to the judge or holds it (fail closed)."""
    global _loaded, _failure
    if _loaded is not None:
        return _loaded
    with _load_lock:
        if _loaded is None and (_failure is None or time.monotonic() - _failure[0] >= RETRY_S):
            try:
                _loaded, _failure = LocalClassifier(), None
            except Exception as e:  # missing model / runtime
                _failure = (time.monotonic(), type(e).__name__)
                log.warning("Tier 1 local classifier unavailable: %s", type(e).__name__)
    return _loaded


def tier1_status() -> dict[str, str | bool]:
    """For readiness checks: loaded, or the last load error."""
    return {"loaded": _loaded is not None, "error": _failure[1] if _failure and _loaded is None else ""}


class RemoteGuard:
    """Prompt Guard 2 via the router (role `tier1_remote`). Returns None when unavailable."""

    def __init__(self, router, role: str = "tier1_remote") -> None:
        self.router = router
        self.role = role
        self._cache: dict[str, float] = {}

    async def score(self, text: str) -> float | None:
        from .llm.adapters import ChatRequest
        from .llm.router import AllProvidersFailed

        if self.role not in self.router.settings.roles:
            return None
        key = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
        if key in self._cache:
            return self._cache[key]
        try:
            r = await self.router.chat(self.role, ChatRequest(messages=[{"role": "user", "content": text[:2000]}],
                                                              max_tokens=8, temperature=None))
            val = float((r.content or "").strip())
        except (AllProvidersFailed, ValueError):
            return None
        self._cache[key] = val
        if len(self._cache) > CACHE_MAX:
            self._cache.pop(next(iter(self._cache)))
        return val
