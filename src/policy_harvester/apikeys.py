"""Public API keys: issued in the admin UI, sent as ``X-API-Key``, stored only as SHA-256.

A key is ``ph_`` plus 32 random bytes (URL-safe base64). Its first characters are kept as a
visible prefix so operators can tell keys apart; the full key is shown once, at creation.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings

KEY_PREFIX = "ph_"
PREFIX_LENGTH = len(KEY_PREFIX) + 8
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False,
                              description="관리자 화면의 API key 메뉴에서 발급한 key")
_windows: dict[str, deque[float]] = defaultdict(deque)
_last_recorded: dict[str, float] = {}
_pending: dict[str, int] = defaultdict(int)
USAGE_WRITE_INTERVAL = 60.0


def generate_key() -> tuple[str, str, str]:
    """Return (full key, visible prefix, sha256 hex)."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:PREFIX_LENGTH], hash_key(key)


def hash_key(key: str) -> str:
    # The key carries 256 random bits, so a plain digest cannot be brute-forced.
    return hashlib.sha256(key.encode()).hexdigest()


async def verify_api_key(session: AsyncSession, key: str | None) -> dict[str, Any] | None:
    """The key row if the key is valid, else raise 401/429. None when keys are not required."""
    if not key:
        if not get_settings().api_key_required:
            return None
        raise HTTPException(401, "X-API-Key header is required", headers={"WWW-Authenticate": "ApiKey"})
    row = (await session.execute(text("""
        SELECT id, name, rate_limit_per_minute, expires_at, revoked_at
        FROM inha_policy.api_keys WHERE key_sha256=:digest
    """), {"digest": hash_key(key)})).mappings().one_or_none()
    now = datetime.now(UTC)
    if row is None or row["revoked_at"] is not None or (row["expires_at"] and row["expires_at"] <= now):
        raise HTTPException(401, "invalid, revoked or expired API key", headers={"WWW-Authenticate": "ApiKey"})
    key_id = str(row["id"])
    window, moment = _windows[key_id], time.monotonic()
    while window and window[0] < moment - 60:
        window.popleft()
    if len(window) >= row["rate_limit_per_minute"]:
        raise HTTPException(429, "API key rate limit exceeded", headers={"Retry-After": "60"})
    window.append(moment)
    _pending[key_id] += 1
    if moment - _last_recorded.get(key_id, float("-inf")) >= USAGE_WRITE_INTERVAL:
        # Usage is written at most once a minute per key and process, so reads stay cheap.
        count, _pending[key_id], _last_recorded[key_id] = _pending[key_id], 0, moment
        await session.execute(text("""
            UPDATE inha_policy.api_keys SET last_used_at=now(), request_count=request_count + :count
            WHERE id=:id
        """), {"id": row["id"], "count": count})
        await session.commit()
    return dict(row)
