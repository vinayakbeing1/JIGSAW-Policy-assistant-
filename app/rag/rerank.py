"""Cross-encoder reranking of retrieved passages.

The hybrid search (app.rag.retriever) finds candidates with two independent signals - embedding similarity
and keyword overlap - and fuses their ranks. A cross-encoder instead reads the question and each candidate
passage together and scores how well the passage answers the question, which orders the final few sources
much more accurately. It is too slow to run over every chunk, so it only re-scores the best fused
candidates (RERANKER_CANDIDATES) and the top RETRIEVAL_TOP_K are given to the model.

The model is a small ONNX cross-encoder run locally with fastembed (no PyTorch, no extra API key, question
text stays on our servers). The Docker image downloads it at build time because the containers run on a
read-only filesystem. If it cannot be loaded the assistant keeps working with the fused order and /ready
reports the problem. Tests replace it through ``use_reranker``.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.log import get_logger

log = get_logger(__name__)


@runtime_checkable
class Reranker(Protocol):
    name: str

    def score(self, query: str, passages: list[str]) -> list[float]:
        """One relevance score per passage, in the same order (higher is more relevant)."""
        ...


class CrossEncoderReranker:
    def __init__(self, model: str, cache_dir: str | None = None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # heavy import, only when enabled

        self.name = model
        self._model = TextCrossEncoder(model_name=model, cache_dir=cache_dir)
        self._lock = threading.Lock()  # the tokenizer is shared; scoring 20 passages takes milliseconds

    def score(self, query: str, passages: list[str]) -> list[float]:
        with self._lock:
            return [float(s) for s in self._model.rerank(query, passages)]


_lock = threading.Lock()
_override: Reranker | None = None
_overridden = False
_loaded: Reranker | None = None
_load_error: str | None = None


def reranker() -> Reranker | None:
    """The reranker to use, or None when reranking is disabled or the model could not be loaded."""
    global _loaded, _load_error
    with _lock:
        if _overridden:
            return _override
        settings = get_settings()
        if not settings.reranker_enabled or _load_error is not None:
            return None
        if _loaded is None:
            cache_dir = str(settings.reranker_cache_dir) if settings.reranker_cache_dir else None
            try:
                _loaded = CrossEncoderReranker(settings.reranker_model, cache_dir)
            except Exception as exc:
                # Not retried until restart: a missing model would otherwise slow down every question.
                _load_error = type(exc).__name__  # shown on /ready, so no message text
                log.error("reranker_unavailable", model=settings.reranker_model, error=f"{exc}"[:500])
                return None
        return _loaded


def status() -> dict[str, object]:
    """Readiness of the reranker (loads the model if it is not loaded yet)."""
    current = reranker()
    if current is not None:
        return {"ok": True, "enabled": True, "model": current.name}
    if _load_error is None:
        return {"ok": True, "enabled": False}
    return {"ok": False, "enabled": True, "model": get_settings().reranker_model, "error": _load_error}


def use_reranker(r: Reranker | None) -> None:
    """Replace the reranker (tests); ``None`` turns reranking off."""
    global _override, _overridden
    with _lock:
        _override, _overridden = r, True


def reset_reranker() -> None:
    """Go back to the configured model (and try loading it again)."""
    global _override, _overridden, _loaded, _load_error
    with _lock:
        _override, _overridden, _loaded, _load_error = None, False, None, None
