"""Application settings, read from environment variables (and .env via docker compose)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ROLES = ("admin", "underwriter", "claims_handler", "customer_service")

# USD per 1M tokens, used for cost estimates only (developers.openai.com, checked 2026-10-12).
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "gpt-6-luna": (0.10, 0.50),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-astra": (10.00, 50.00),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    session_ttl_hours: int = Field(12, ge=1, le=24 * 14)

    database_url: str = "postgresql+psycopg://jigsaw_app@localhost:5432/jigsaw_policy"
    storage_dir: Path = PROJECT_ROOT / "data" / "uploads"
    max_upload_mb: int = Field(25, ge=1, le=200)
    max_pdf_pages: int = Field(500, ge=1)

    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    openai_chat_model: str = "gpt-6-luna"
    openai_reasoning_effort: Literal["none", "low", "medium", "high"] | None = "low"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(1536, ge=64, le=3072)
    openai_timeout_seconds: float = Field(30.0, ge=1.0)
    openai_max_retries: int = Field(2, ge=0, le=6)

    chunk_size: int = Field(1200, ge=200, le=8000)  # characters
    chunk_overlap: int = Field(150, ge=0, le=2000)
    ocr_language: str = "eng"
    ocr_dpi: int = Field(300, ge=100, le=600)

    retrieval_top_k: int = Field(6, ge=1, le=30)
    retrieval_candidates: int = Field(30, ge=5, le=200)
    retrieval_min_similarity: float = Field(0.25, ge=0.0, le=1.0)
    grounding_judge: bool = True

    # Cross-encoder reranking of the fused hybrid-search hits (local ONNX model, see app/rag/rerank.py).
    reranker_enabled: bool = True
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    reranker_candidates: int = Field(20, ge=1, le=100)  # fused hits re-scored; the best top_k are kept
    reranker_cache_dir: Path | None = None  # the Docker image bakes the model into /opt/models

    worker_poll_seconds: float = Field(2.0, ge=0.1)
    job_max_attempts: int = Field(3, ge=1, le=10)

    seed_password: SecretStr | None = None

    @field_validator("openai_reasoning_effort", mode="before")
    @classmethod
    def _omit_effort(cls, v: object) -> object:
        return None if isinstance(v, str) and v.strip().lower() in ("", "omit") else v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.app_env == "production" and not self.public_base_url.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use https:// in production")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.public_base_url.startswith("https://")

    @property
    def openai_configured(self) -> bool:
        key = self.openai_api_key.get_secret_value() if self.openai_api_key else ""
        return bool(key) and key != "CHANGE_ME"


@lru_cache
def get_settings() -> Settings:
    return Settings()
