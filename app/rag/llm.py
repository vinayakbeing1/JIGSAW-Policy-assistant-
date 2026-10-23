"""LangChain model clients (OpenAI) and cost accounting.

The chat model and embeddings are created once per process. Tests replace them with offline fakes through
``use_models``; nothing else in the code base knows which provider is behind them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import openai
from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.config import MODEL_PRICES, get_settings


class ModelError(Exception):
    """A model call failed. ``code`` and ``message`` are safe to show to users."""

    def __init__(self, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


_lock = threading.Lock()
_chat: BaseChatModel | None = None
_embeddings: Embeddings | None = None


def _api_key() -> str:
    settings = get_settings()
    if not settings.openai_configured:
        raise ModelError("model_not_configured", "OPENAI_API_KEY is not set. Ask an administrator.", False)
    return settings.openai_api_key.get_secret_value()  # type: ignore[union-attr]


def chat_model() -> BaseChatModel:
    global _chat
    with _lock:
        if _chat is None:
            s = get_settings()
            _chat = ChatOpenAI(
                model=s.openai_chat_model,
                api_key=_api_key(),
                base_url=s.openai_base_url,
                timeout=s.openai_timeout_seconds,
                max_retries=s.openai_max_retries,
                reasoning_effort=s.openai_reasoning_effort,
                use_responses_api=True,
                store=False,  # do not keep responses on OpenAI's side
            )
        return _chat


def embeddings() -> Embeddings:
    global _embeddings
    with _lock:
        if _embeddings is None:
            s = get_settings()
            _embeddings = OpenAIEmbeddings(
                model=s.openai_embedding_model,
                dimensions=s.embedding_dimensions,
                api_key=_api_key(),
                base_url=s.openai_base_url,
                timeout=s.openai_timeout_seconds,
                max_retries=s.openai_max_retries,
                # Chunks are far below the model's input limit, so skip tiktoken pre-splitting (it would
                # download its encoding files at runtime).
                check_embedding_ctx_length=False,
            )
        return _embeddings


def embedding_model_name() -> str:
    return getattr(embeddings(), "model", None) or get_settings().openai_embedding_model


def use_models(chat: BaseChatModel | None, embedder: Embeddings | None) -> None:
    """Replace the model clients (tests). ``None`` resets to the configured OpenAI clients."""
    global _chat, _embeddings
    with _lock:
        _chat, _embeddings = chat, embedder


def model_error(exc: Exception) -> ModelError:
    """Translate provider exceptions into messages a user can act on."""
    if isinstance(exc, ModelError):
        return exc
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ModelError(
            "model_auth_failed", "The OpenAI API key was rejected. Ask an administrator.", False
        )
    if isinstance(exc, openai.NotFoundError | openai.BadRequestError):
        return ModelError(
            "model_request_rejected", "The model rejected the request (check model settings).", False
        )
    if isinstance(exc, openai.RateLimitError):
        return ModelError(
            "model_rate_limited", "The model is busy right now. Please try again shortly.", True
        )
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError | openai.InternalServerError):
        return ModelError("model_unavailable", "The model did not respond. Please try again.", True)
    return ModelError("model_error", "The model returned an unexpected response. Please try again.", True)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model, tokens in self.by_model.items():
            price = next((p for name, p in MODEL_PRICES.items() if model.startswith(name)), (0.0, 0.0))
            total += (tokens["input"] * price[0] + tokens["output"] * price[1]) / 1_000_000
        return total


@contextmanager
def track_usage() -> Iterator[Usage]:
    """Collect token usage of every chat call made inside the block (LangChain usage callback)."""
    usage = Usage()
    with get_usage_metadata_callback() as cb:
        try:
            yield usage
        finally:
            for model, meta in cb.usage_metadata.items():
                tokens = {"input": meta.get("input_tokens", 0), "output": meta.get("output_tokens", 0)}
                usage.by_model[model] = tokens
                usage.input_tokens += tokens["input"]
                usage.output_tokens += tokens["output"]
