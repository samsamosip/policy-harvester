"""Embedding profiles: one per (provider, model, dimensions, chunker), created on demand."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CHUNKER_VERSION = "policy-summary-1.0"


@dataclass(frozen=True)
class EmbeddingProfile:
    id: uuid.UUID
    key: str
    provider: str
    model: str
    dimensions: int
    is_active: bool


async def ensure_profile(session: AsyncSession, config: dict[str, dict[str, Any]]) -> EmbeddingProfile:
    """The profile for the effective embedding settings; the first profile ever becomes active."""
    provider = config["embedding.provider"]["value"]
    model = config["embedding.model"]["value"]
    dimensions = int(config["embedding.dimensions"]["value"])
    profile_config = {"provider": provider, "model": model, "dimensions": dimensions,
                      "normalized": True, "distance_metric": "cosine", "chunker": CHUNKER_VERSION}
    key = f"{provider}:{model}:{dimensions}:{CHUNKER_VERSION}"
    row = (await session.execute(text("""
        INSERT INTO inha_policy.embedding_profiles
          (profile_key, model_id, dimensions, input_template_version, chunker_version,
           tokenizer_version, config_sha256, config, is_active)
        VALUES (:key, :model, :dimensions, 'opportunity-1.0', :chunker,
                'provider-default', :hash, CAST(:config AS jsonb),
                NOT EXISTS (SELECT 1 FROM inha_policy.embedding_profiles WHERE is_active))
        ON CONFLICT (profile_key) DO UPDATE SET profile_key=EXCLUDED.profile_key
        RETURNING id, is_active
    """), {"key": key, "model": model, "dimensions": dimensions, "chunker": CHUNKER_VERSION,
             "hash": hashlib.sha256(json.dumps(profile_config, sort_keys=True).encode()).hexdigest(),
             "config": json.dumps(profile_config)})).mappings().one()
    return EmbeddingProfile(row["id"], key, provider, model, dimensions, row["is_active"])


async def coverage(session: AsyncSession, profile_id: uuid.UUID) -> dict[str, int]:
    """How many latest opportunity versions (draft or published) have a vector in the profile."""
    row = (await session.execute(text("""
        WITH latest AS (
          SELECT DISTINCT ON (opportunity_id) id, publication_state FROM inha_policy.opportunity_versions
          ORDER BY opportunity_id, version_no DESC)
        SELECT count(*) AS total,
               count(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM inha_policy.search_chunks sc WHERE sc.opportunity_version_id=latest.id
                   AND sc.embedding_profile_id=:profile AND sc.embedding_status='succeeded')) AS embedded,
               count(*) FILTER (WHERE EXISTS (
                 SELECT 1 FROM inha_policy.crawl_jobs j WHERE j.stage='embed' AND j.status IN ('queued','retry','running')
                   AND j.payload->>'opportunity_version_id' = latest.id::text)) AS queued
        FROM latest
    """), {"profile": profile_id})).mappings().one()
    return dict(row)


async def activate_if_complete(session: AsyncSession, profile: EmbeddingProfile) -> bool:
    """Switch search to ``profile`` once every latest opportunity version has a vector in it."""
    if profile.is_active:
        return False
    counts = await coverage(session, profile.id)
    if counts["total"] == 0 or counts["embedded"] < counts["total"]:
        return False
    await session.execute(text("SELECT pg_advisory_xact_lock(7410932176502::bigint)"))
    before = (await session.execute(text(
        "SELECT profile_key FROM inha_policy.embedding_profiles WHERE is_active"))).scalar_one_or_none()
    await session.execute(text("UPDATE inha_policy.embedding_profiles SET is_active=false WHERE is_active"))
    await session.execute(text("UPDATE inha_policy.embedding_profiles SET is_active=true WHERE id=:id"),
                          {"id": profile.id})
    await session.execute(text("""
        INSERT INTO inha_policy.audit_logs
          (actor_kind, actor_id, action, entity_type, entity_id, before_state, after_state, automated)
        VALUES ('worker', 'embedder', 'embedding_profile_activated', 'embedding_profile', :id,
                jsonb_build_object('profile_key', CAST(:before AS text)),
                jsonb_build_object('profile_key', CAST(:after AS text), 'embedded', CAST(:count AS integer)), true)
    """), {"id": str(profile.id), "before": before, "after": profile.key, "count": counts["embedded"]})
    return True
