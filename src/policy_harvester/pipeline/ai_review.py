"""AI second review: a stronger model judges review items before people see them.

Items that hinge on model judgement (quality, identity, revision candidates) are created in the
same transaction as the extraction that raised them; ``defer`` turns them into ``ai_pending`` and
queues a ``review`` job. The job shows the model the item, the opportunities involved and the
notice text. A confident "dismiss" closes the item (and, for quality, lets the version publish);
anything else opens it for people with the model's reasoning attached.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SOURCE_CHAR_LIMIT = 60000
# Review items created while a job runs, collected on the session by whoever inserts them.
CREATED_REVIEWS = "created_review_ids"


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["dismiss", "escalate"]
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(max_length=2000)

    @property
    def dismissed(self) -> bool:
        return self.verdict == "dismiss" and self.confidence == "high"


def remember_created(session: AsyncSession, review_id: uuid.UUID) -> None:
    session.info.setdefault(CREATED_REVIEWS, []).append(review_id)


def reviewable(kind: str, payload: dict[str, Any]) -> bool:
    """Judgement calls a model can settle. Failures (parsing, extraction, embedding) and database
    guard refusals are facts about the system, not judgements, and go straight to people."""
    if kind in {"identity_uncertain", "revision_candidate"}:
        return True
    return kind == "other" and "quality_flags" in payload


def issue(kind: str, payload: dict[str, Any]) -> str:
    """Why the item was raised and what the system did meanwhile ("dismiss" keeps that)."""
    operation = payload.get("operation")
    if kind == "other":
        return ("품질(quality): 자동 공개 기준을 통과하지 못해 초안으로 남겨 둠. "
                "추출 경고(warnings)나 coverage 부족이 실제 데이터 오류인지 확인.")
    if kind == "revision_candidate":
        return ("정정·연장 후보(revision): 이 공고를 독립 공고로 처리하면서, 다른 공고의 정정·연장일 수 있다고 표시함. "
                "실제로 이전 공고를 바꾸는 공고인지 확인.")
    if operation == "same_slot_renamed":
        return ("동일성(identity): 같은 게시글을 다시 추출했더니 장학 이름이 바뀌었는데, 게시글에 장학이 하나뿐이라 "
                "기존 장학의 새 version으로 연결함(현재 처리: 같은 장학). 정말 같은 장학인지 확인.")
    if operation == "same_notice_unmatched":
        return ("동일성(identity): 같은 게시글을 다시 추출했더니 기존 장학과 이름이 달라 새 장학을 만듦"
                "(현재 처리: 별도 장학). 기존 장학 중 같은 것이 있는지 확인.")
    if operation == "cross_notice_candidates":
        return ("동일성(identity): 다른 공고에서 나온 비슷한 장학이 있어 병합 후보로 제안함"
                "(현재 처리: 병합하지 않고 별도 장학). 후보 중 같은 모집이 있는지 확인.")
    return ("동일성(identity): 추출 모델이 이 공고를 기존 장학과 관련된 공고로 보았음"
            "(현재 처리: 별도 장학). 같은 모집인지 확인.")


def related_opportunity_ids(review: dict[str, Any]) -> list[str]:
    payload = review["payload"]
    ids = [str(review["opportunity_id"])] if review["opportunity_id"] else []
    ids += [str(item) for item in payload.get("existing_opportunity_ids") or []]
    ids += [str(item["opportunity_id"]) for item in payload.get("candidates") or [] if item.get("opportunity_id")]
    if (payload.get("comparison") or {}).get("opportunity_id"):
        ids.append(str(payload["comparison"]["opportunity_id"]))
    return list(dict.fromkeys(ids))


async def defer(session: AsyncSession, review_ids: list[uuid.UUID], job: dict[str, Any]) -> int:
    """Hold back the reviewable items among ``review_ids`` and queue a review job for each."""
    rows = (await session.execute(text("""
        SELECT id, review_kind, payload FROM inha_policy.review_items
        WHERE id = ANY(:ids) AND status='open'
    """), {"ids": review_ids})).mappings().all()
    deferred = 0
    for row in rows:
        if not reviewable(row["review_kind"], row["payload"]):
            continue
        await session.execute(text(
            "UPDATE inha_policy.review_items SET status='ai_pending', updated_at=clock_timestamp() WHERE id=:id"
        ), {"id": row["id"]})
        await session.execute(text("""
            INSERT INTO inha_policy.crawl_jobs
              (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
            VALUES (:run, 'review', :key, :notice, :version, CAST(:payload AS jsonb))
            ON CONFLICT DO NOTHING
        """), {"run": job["crawl_run_id"], "key": f"review:{row['id']}", "notice": job.get("notice_id"),
                 "version": job.get("notice_version_id"),
                 "payload": json.dumps({"review_id": str(row["id"])})})
        deferred += 1
    return deferred


async def subject(session: AsyncSession, review: dict[str, Any]) -> tuple[dict[str, Any], uuid.UUID | None]:
    """What the model sees besides the notice text, and the notice version that text comes from."""
    payload = review["payload"]
    opportunities = []
    for opportunity_id in related_opportunity_ids(review):
        version = (await session.execute(text("""
            SELECT ov.id, ov.opportunity_id, ov.title, ov.provider_name, ov.academic_year,
                   ov.academic_term, ov.round_label, ov.support_summary, ov.eligibility_summary,
                   ov.selection_capacity, ov.data_quality_status, ov.publication_state,
                   (SELECT string_agg(DISTINCT nv.title, ' / ') FROM inha_policy.opportunity_version_sources s
                      JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                     WHERE s.opportunity_version_id=ov.id) AS source_notices,
                   (SELECT json_agg(json_build_object('kind', w.window_kind, 'start', w.start_date,
                                                      'end', w.end_date, 'text', w.raw_text))
                      FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id) AS windows
            FROM inha_policy.opportunity_versions ov WHERE ov.opportunity_id=CAST(:id AS uuid)
            ORDER BY ov.version_no DESC LIMIT 1
        """), {"id": opportunity_id})).mappings().one_or_none()
        if version:
            opportunities.append({key: (str(value) if isinstance(value, uuid.UUID) else value)
                                  for key, value in version.items()})
    notice_version_id: uuid.UUID | None = None
    extracted_item = None
    if payload.get("notice_version_id"):
        notice_version_id = uuid.UUID(str(payload["notice_version_id"]))
    elif review["entity_type"] == "notice_version":
        notice_version_id = review["entity_id"]
    source_version = (review["entity_id"] if review["entity_type"] == "opportunity_version"
                      else payload.get("opportunity_version_id"))
    if source_version:
        source = (await session.execute(text("""
            SELECT s.notice_version_id, er.parsed_output, s.extraction_item_path
            FROM inha_policy.opportunity_version_sources s
            JOIN inha_policy.extraction_runs er ON er.id=s.extraction_run_id
            WHERE s.opportunity_version_id=CAST(:id AS uuid) LIMIT 1
        """), {"id": str(source_version)})).mappings().one_or_none()
        if source:
            notice_version_id = notice_version_id or source["notice_version_id"]
            path = (source["extraction_item_path"] or "").strip("/").split("/")
            if len(path) == 2 and path[0] == "opportunities" and path[1].isdigit() and source["parsed_output"]:
                items = source["parsed_output"].get("opportunities") or []
                if int(path[1]) < len(items):
                    extracted_item = items[int(path[1])]
    details = {key: value for key, value in payload.items() if key not in {"ai_review"}}
    return {"review_kind": review["review_kind"], "issue": issue(review["review_kind"], payload),
            "details": details, "opportunities": opportunities,
            "extracted_item": extracted_item}, notice_version_id


def source_text(blocks: dict[str, str]) -> str:
    joined = "\n\n".join(value for value in blocks.values() if value)
    if len(joined) > SOURCE_CHAR_LIMIT:
        return joined[:SOURCE_CHAR_LIMIT] + "\n…(이하 생략)"
    return joined


async def apply(session: AsyncSession, review: dict[str, Any], verdict: ReviewVerdict, model: str) -> str:
    """Record the verdict; a confident dismissal closes the item and undoes what it held back."""
    record = {"model": model, "verdict": verdict.verdict, "confidence": verdict.confidence,
              "reason": verdict.reason}
    if not verdict.dismissed:
        await open_item(session, review["id"], record)
        return "escalated"
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status='dismissed', resolved_at=clock_timestamp(),
          updated_at=clock_timestamp(), resolution_note=:note,
          payload = payload || jsonb_build_object('ai_review', CAST(:record AS jsonb))
        WHERE id=:id AND status='ai_pending'
    """), {"id": review["id"], "note": f"AI 2차 검토({model}): {verdict.reason}",
             "record": json.dumps(record, ensure_ascii=False)})
    payload = review["payload"]
    if review["review_kind"] == "other" and review["entity_type"] == "opportunity_version":
        await _clear_quality(session, review)
    if payload.get("operation") == "cross_notice_candidates":
        for candidate in payload.get("candidates") or []:
            await _reject_proposal(session, candidate.get("decision_id"), verdict.reason)
    await session.execute(text("""
        INSERT INTO inha_policy.audit_logs
          (actor_kind, actor_id, action, entity_type, entity_id, after_state, reason, automated)
        VALUES ('worker', 'ai_reviewer', 'review_dismissed_by_ai', 'review_item', CAST(:id AS text),
                CAST(:record AS jsonb), :reason, true)
    """), {"id": review["id"], "record": json.dumps(record, ensure_ascii=False), "reason": verdict.reason})
    return "dismissed"


async def open_item(session: AsyncSession, review_id: uuid.UUID, record: dict[str, Any]) -> None:
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status='open', updated_at=clock_timestamp(),
          payload = payload || jsonb_build_object('ai_review', CAST(:record AS jsonb))
        WHERE id=:id AND status='ai_pending'
    """), {"id": review_id, "record": json.dumps(record, ensure_ascii=False)})


async def _clear_quality(session: AsyncSession, review: dict[str, Any]) -> None:
    """The model found the flagged draft correct: give it the quality the flags withheld and
    re-run the embed step, which publishes it when auto-publishing is on."""
    from .assembler import OpportunityAssembler

    source = (await session.execute(text("""
        SELECT ov.id, s.notice_version_id, nv.notice_id, nv.crawl_run_id
        FROM inha_policy.opportunity_versions ov
        JOIN inha_policy.opportunity_version_sources s ON s.opportunity_version_id=ov.id
        JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
        WHERE ov.id=:id AND ov.publication_state='draft' AND ov.published_at IS NULL
          AND ov.version_no=(SELECT max(version_no) FROM inha_policy.opportunity_versions
                             WHERE opportunity_id=ov.opportunity_id)
        LIMIT 1
    """), {"id": review["entity_id"]})).mappings().one_or_none()
    if source is None:  # published, superseded or gone meanwhile: nothing to release
        return
    provenance = await OpportunityAssembler(session)._provenance_flags(source["notice_version_id"])
    await session.execute(text("""
        UPDATE inha_policy.opportunity_versions
        SET data_quality_status=:quality,
            quality_flags=array_cat(quality_flags, CAST(:flags AS text[]))
        WHERE id=:id
    """), {"id": source["id"], "quality": "partial" if provenance else "complete",
             "flags": ["ai_review_cleared", *provenance]})
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:run, 'embed', :key, :notice, :version, CAST(:payload AS jsonb))
        ON CONFLICT DO NOTHING
    """), {"run": source["crawl_run_id"], "key": f"opportunity:{source['id']}:ai-review",
             "notice": source["notice_id"], "version": source["notice_version_id"],
             "payload": json.dumps({"opportunity_version_id": str(source["id"])})})


async def _reject_proposal(session: AsyncSession, decision_id: str | None, reason: str) -> None:
    if not decision_id:
        return
    proposal = (await session.execute(text("""
        SELECT decision_kind, opportunity_id, other_opportunity_id FROM inha_policy.identity_decisions p
        WHERE id=CAST(:id AS uuid) AND decision_status='proposed'
          AND NOT EXISTS (SELECT 1 FROM inha_policy.identity_decisions d
                          WHERE d.supersedes_decision_id=p.id)
    """), {"id": decision_id})).mappings().one_or_none()
    if proposal is None:
        return
    await session.execute(text("""
        INSERT INTO inha_policy.identity_decisions
          (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
           supersedes_decision_id, identity_basis, rule_version, decision_reason, actor_kind,
           actor_id, observed_at)
        VALUES (gen_random_uuid(), :kind, 'rejected', :opportunity, :other, CAST(:proposal AS uuid),
                'insufficient', 'ai-review-1.0', :reason, 'llm_suggestion', 'ai_reviewer', clock_timestamp())
    """), {"kind": proposal["decision_kind"], "opportunity": proposal["opportunity_id"],
             "other": proposal["other_opportunity_id"], "proposal": decision_id, "reason": reason})
