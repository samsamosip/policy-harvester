"""Persist raw LLM request/response pairs: bodies in the object store, an index row in the DB."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..storage import ObjectStore

PURPOSES = {"extraction", "repair", "transcription"}


def _jsonable(value: Any) -> Any:
    if isinstance(value, type):
        schema = getattr(value, "model_json_schema", None)
        return {"python_type": value.__name__, "json_schema": schema()} if schema else value.__name__
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode()}
    return repr(value)


def _put_json(store: ObjectStore, value: Any) -> tuple[str, str]:
    stored = store.put(json.dumps(value, ensure_ascii=False, default=_jsonable).encode())
    return stored.storage_key, stored.sha256


async def persist_exchanges(session: AsyncSession, store: ObjectStore, log: list[dict[str, Any]], *,
                            notice_version_id: uuid.UUID | None,
                            extraction_run_id: uuid.UUID | None = None) -> int:
    for entry in log:
        request_key, request_sha = _put_json(store, entry["request"])
        response_key = response_sha = None
        if entry.get("response") is not None:
            response_key, response_sha = _put_json(store, entry["response"])
        usage = (entry.get("response") or {}).get("usage") if isinstance(entry.get("response"), dict) else None
        usage = usage or {}
        await session.execute(text("""
            INSERT INTO inha_policy.llm_exchanges
              (purpose, provider_name, model_id, notice_version_id, extraction_run_id, status,
               error_message, request_storage_key, request_sha256, response_storage_key,
               response_sha256, latency_ms, input_tokens, output_tokens, started_at)
            VALUES (:purpose, :provider, :model, :version, :run, :status, :error, :req_key, :req_sha,
                    :res_key, :res_sha, :latency, :tin, :tout, CAST(:started AS timestamptz))
        """), {
            "purpose": entry["purpose"] if entry["purpose"] in PURPOSES else "other",
            "provider": entry["provider"], "model": entry.get("model") or "unknown",
            "version": notice_version_id,
            "run": (entry.get("extraction_run_id") or extraction_run_id)
                   if entry["purpose"] in {"extraction", "repair"} else None,
            "status": entry.get("status", "error"), "error": entry.get("error"),
            "req_key": request_key, "req_sha": request_sha, "res_key": response_key, "res_sha": response_sha,
            "latency": entry.get("latency_ms"),
            "tin": usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("prompt_token_count"),
            "tout": usage.get("completion_tokens") or usage.get("output_tokens")
            or usage.get("candidates_token_count"),
            "started": entry["started_at"]})
    return len(log)
