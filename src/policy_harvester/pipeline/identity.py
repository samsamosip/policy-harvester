"""Cross-notice identity candidates.

A new opportunity is compared with opportunities that came from other notices. Close
matches are written as *proposed* merge decisions plus a review item; nothing is merged
automatically, and similarity alone never confirms identity.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .ai_review import remember_created
from .edition import compare, version_signature

MAX_CANDIDATES = 3


class IdentityCandidateService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def propose_for_versions(self, version_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        proposals: list[uuid.UUID] = []
        for version_id in version_ids:
            proposals.extend(await self.propose_for_version(version_id))
        return proposals

    async def propose_for_version(self, version_id: uuid.UUID) -> list[uuid.UUID]:
        version = (await self.session.execute(text("""
            SELECT ov.id, ov.opportunity_id, ov.title, ov.provider_name, ov.academic_year,
                   ov.academic_term, ov.round_label, ov.edit_kind, ov.observed_at, o.program_key
            FROM inha_policy.opportunity_versions ov
            JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id WHERE ov.id=:id
        """), {"id": version_id})).mappings().one()
        if version["edit_kind"] != "initial":
            return []
        # The SQL filter only narrows the field; edition.compare makes the call.
        rows = (await self.session.execute(text("""
            WITH own_notices AS (
                SELECT nv.notice_id FROM inha_policy.opportunity_version_sources s
                JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                WHERE s.opportunity_version_id=:version
            ), latest AS (
                SELECT DISTINCT ON (ov.opportunity_id) ov.id, ov.opportunity_id, ov.title,
                       ov.provider_name, ov.academic_year, ov.academic_term, ov.round_label,
                       o.program_key
                FROM inha_policy.opportunity_versions ov
                JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
                WHERE o.lifecycle_status <> 'merged' AND o.id <> :opportunity
                ORDER BY ov.opportunity_id, ov.version_no DESC
            )
            SELECT latest.* FROM latest
            WHERE (similarity(latest.title, :title) >= 0.2 OR latest.program_key = :program_key)
              AND NOT EXISTS (
                SELECT 1 FROM inha_policy.opportunity_version_sources s
                JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                WHERE s.opportunity_version_id=latest.id
                  AND nv.notice_id IN (SELECT notice_id FROM own_notices))
            ORDER BY similarity(latest.title, :title) DESC LIMIT 50
        """), {"version": version_id, "opportunity": version["opportunity_id"],
                 "title": version["title"], "program_key": version["program_key"]})).mappings().all()
        mine = await version_signature(self.session, version)
        candidates = []
        for row in rows:
            result = compare(mine, await version_signature(self.session, row))
            # Other editions of the same program share program_key; they are never merged.
            if result.verdict in {"same", "uncertain"}:
                candidates.append((row, result))
        order = {"same": 0, "uncertain": 1}
        candidates.sort(key=lambda item: (order[item[1].verdict], -item[1].name_score))
        decisions = []
        for row, result in candidates[:MAX_CANDIDATES]:
            decision_id = uuid.uuid4()
            await self.session.execute(text("""
                INSERT INTO inha_policy.identity_decisions
                  (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
                   identity_basis, scope, rule_version, decision_reason, actor_kind, observed_at)
                VALUES (:id, 'merge', 'proposed', :existing, :new, 'insufficient',
                        CAST(:scope AS jsonb), :rule, :reason, 'deterministic_rule', :observed)
            """), {"id": decision_id, "existing": row["opportunity_id"],
                     "new": version["opportunity_id"], "rule": result.rule_version,
                     "scope": json.dumps(result.as_json(), ensure_ascii=False),
                     "reason": "; ".join(result.reasons), "observed": version["observed_at"]})
            decisions.append({"decision_id": str(decision_id),
                              "opportunity_id": str(row["opportunity_id"]), "title": row["title"],
                              "score": round(result.name_score, 4), "verdict": result.verdict,
                              "reasons": list(result.reasons)})
        if decisions:
            review_id = (await self.session.execute(text("""
                INSERT INTO inha_policy.review_items
                  (review_kind, entity_type, entity_id, opportunity_id, payload)
                VALUES ('identity_uncertain', 'opportunity', :id, :id, CAST(:payload AS jsonb))
                RETURNING id
            """), {"id": version["opportunity_id"], "payload": json.dumps({
                "operation": "cross_notice_candidates", "opportunity_version_id": str(version_id),
                "title": version["title"], "candidates": decisions}, ensure_ascii=False)})).scalar_one()
            remember_created(self.session, review_id)
        return [uuid.UUID(item["decision_id"]) for item in decisions]
