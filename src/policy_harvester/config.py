from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .security import decrypt_secret, validate_proxy_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_env: Literal["local", "staging", "production"] = "local"
    database_url: str = "postgresql+psycopg://policy:policy@localhost:5432/policy"
    public_base_url: str = "http://localhost:8000"
    session_secret: SecretStr = SecretStr("local-development-only-change-me")
    master_key: SecretStr | None = None

    object_store_backend: Literal["filesystem", "s3"] = "filesystem"
    object_store_root: Path = Path("./data/objects")
    s3_endpoint_url: str | None = None
    s3_bucket: str = "policy-raw"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    s3_auto_create_bucket: bool = False

    llm_provider: str = "openai"
    llm_model: str = "gpt-5-mini"
    llm_api_key: SecretStr | None = None
    llm_base_url: str | None = None
    llm_timeout_seconds: float = 120
    llm_max_retries: int = 2
    # Stream chat completions so gateways that drop long idle requests (~10 min) keep the connection.
    llm_stream: bool = False

    # Output cap for structured extraction (and repair) calls. Without it some providers stop at
    # their default (~64k), which long multi-track notices exceed when reasoning is on.
    llm_max_output_tokens: int | None = Field(131072, ge=1024)

    # Structured extraction may use its own model and endpoint; unset fields fall back to llm_*.
    # Image transcription uses llm_*; the cross-check and AI review have their own optional ones.
    extraction_llm_provider: str | None = None
    extraction_llm_model: str | None = None
    extraction_llm_base_url: str | None = None
    extraction_llm_api_key: SecretStr | None = None
    extraction_reasoning_effort: str | None = Field(None, pattern="^(low|medium|high)$")
    # Second model run when the input looks multi-track or extraction fails. Its own endpoint
    # fields are optional; unset ones fall back to llm_*.
    extraction_crosscheck_model: str | None = None
    crosscheck_llm_provider: str | None = None
    crosscheck_llm_base_url: str | None = None
    crosscheck_llm_api_key: SecretStr | None = None
    # Review items that hinge on model judgement (quality, identity, revision candidates) are
    # first judged by this model on the llm_* endpoint; only what it still finds questionable
    # reaches people. Unset: items go straight to the review queue.
    review_llm_model: str | None = None
    review_reasoning_effort: str | None = Field("high", pattern="^(low|medium|high)$")
    review_llm_provider: str | None = None
    review_llm_base_url: str | None = None
    review_llm_api_key: SecretStr | None = None

    # The public /v1 API requires an X-API-Key issued in the admin UI. Turn off only for local use.
    api_key_required: bool = True

    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_api_key: SecretStr | None = None
    embedding_base_url: str | None = None
    # embedding_provider "onnx" runs one of ai/onnx_embeddings.ONNX_MODELS locally on the CPU.
    onnx_threads: int | None = Field(None, ge=1, le=256)

    crawl_user_agent: str = "PolicyHarvester/0.1"
    crawl_timeout_seconds: float = 45
    crawl_max_concurrency: int = Field(4, ge=1, le=32)
    crawl_proxy_url: SecretStr | None = None
    crawl_tls_verify: bool = True
    source_missing_threshold: int = Field(3, ge=2, le=30)
    source_timezone: str = "Asia/Seoul"
    auto_publish: bool = False

    @field_validator("database_url")
    @classmethod
    def normalize_database_driver(cls, value: str) -> str:
        return value.replace("postgresql://", "postgresql+psycopg://", 1)

    @field_validator("crawl_proxy_url")
    @classmethod
    def validate_crawl_proxy(cls, value: SecretStr | None) -> SecretStr | None:
        if value and value.get_secret_value():
            validate_proxy_url(value.get_secret_value())
        return value

    @model_validator(mode="after")
    def validate_deployment_secrets(self):
        if self.app_env != "local" and self.session_secret.get_secret_value() == "local-development-only-change-me":
            raise ValueError("SESSION_SECRET must be changed outside local development")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


RUNTIME_KEYS: dict[str, str] = {
    "llm.provider": "llm_provider",
    "llm.base_url": "llm_base_url",
    "llm.model": "llm_model",
    "llm.timeout_seconds": "llm_timeout_seconds",
    "llm.max_retries": "llm_max_retries",
    "llm.stream": "llm_stream",
    "llm.max_output_tokens": "llm_max_output_tokens",
    "extraction.provider": "extraction_llm_provider",
    "extraction.base_url": "extraction_llm_base_url",
    "extraction.model": "extraction_llm_model",
    "extraction.reasoning_effort": "extraction_reasoning_effort",
    "extraction.crosscheck_model": "extraction_crosscheck_model",
    "crosscheck.provider": "crosscheck_llm_provider",
    "crosscheck.base_url": "crosscheck_llm_base_url",
    "review.provider": "review_llm_provider",
    "review.base_url": "review_llm_base_url",
    "review.model": "review_llm_model",
    "review.reasoning_effort": "review_reasoning_effort",
    "embedding.provider": "embedding_provider",
    "embedding.base_url": "embedding_base_url",
    "embedding.model": "embedding_model",
    "embedding.dimensions": "embedding_dimensions",
    "publishing.auto_publish": "auto_publish",
}


async def effective_configuration(session: AsyncSession) -> dict[str, dict[str, Any]]:
    base = get_settings()
    rows = (await session.execute(text(
        "SELECT setting_key, value_json FROM inha_policy.runtime_settings"
    ))).mappings()
    overrides = {row["setting_key"]: row["value_json"] for row in rows}
    result: dict[str, dict[str, Any]] = {}
    for public_key, attribute in RUNTIME_KEYS.items():
        default_value = getattr(base, attribute)
        if public_key in overrides:
            result[public_key] = {"value": overrides[public_key], "source": "admin_override"}
        else:
            source = "environment" if attribute in base.model_fields_set else "application_default"
            result[public_key] = {"value": default_value, "source": source}
    return result


async def resolved_settings(session: AsyncSession) -> Settings:
    base = get_settings()
    effective = await effective_configuration(session)
    updates = {RUNTIME_KEYS[key]: item["value"] for key, item in effective.items()}
    master = base.master_key.get_secret_value() if base.master_key else ""
    if master:
        rows = (await session.execute(text("""
            SELECT DISTINCT ON (secret_key) secret_key, ciphertext
            FROM inha_policy.encrypted_secrets WHERE is_active
            ORDER BY secret_key, version_no DESC
        """))).mappings().all()
        secret_fields = {"llm.api_key": "llm_api_key", "embedding.api_key": "embedding_api_key",
                         "extraction.api_key": "extraction_llm_api_key",
                         "crosscheck.api_key": "crosscheck_llm_api_key",
                         "review.api_key": "review_llm_api_key"}
        for row in rows:
            field = secret_fields.get(row["secret_key"])
            if field:
                updates[field] = decrypt_secret(bytes(row["ciphertext"]), master, row["secret_key"])
    return Settings.model_validate({**base.model_dump(), **updates})


async def source_proxy_url(session: AsyncSession, source_key: str) -> str | None:
    """Resolve a source credential without ever placing it in runtime_settings."""
    base = get_settings()
    fallback = base.crawl_proxy_url.get_secret_value() if base.crawl_proxy_url else None
    master = base.master_key.get_secret_value() if base.master_key else ""
    if not master:
        return fallback or None
    secret_key = f"crawl.proxy.{source_key}"
    row = (await session.execute(text("""
        SELECT ciphertext FROM inha_policy.encrypted_secrets
        WHERE secret_key=:key AND is_active ORDER BY version_no DESC LIMIT 1
    """), {"key": secret_key})).scalar_one_or_none()
    if row is None:
        return fallback or None
    return decrypt_secret(bytes(row), master, secret_key)
