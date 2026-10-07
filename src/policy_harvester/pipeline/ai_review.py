"""AI second review: a stronger model judges review items before people see them.

Items that hinge on model judgement (quality, identity, revision candidates) are created in the
same transaction as the extraction that raised them; ``defer`` turns them into ``ai_pending`` and
queues a ``review`` job. The job shows the model the item, the opportunities involved and the
notice text. With high confidence the model acts instead of people:
- dismiss: nothing is wrong; the item closes (a quality item's draft is released for publication);
- fix: a quality item's draft has wrong values; they are corrected from the source, then released;
- merge: an identity item's opportunities are one; they are merged into the oldest published one;
- revise: a revision candidate amends an earlier opportunity; the amendment is applied to it as a
  new version (and the amendment notice's own copy of that opportunity is merged into it).
Anything else opens the item for people with the model's reasoning attached.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

SOURCE_CHAR_LIMIT = 60000
AMENDMENT_MARKERS = re.compile(r"[\(\[]\s*(?:수정|정정|변경|재공고|추가모집|(?:신청)?(?:기간|기한)\s*연장|연장)\s*[\)\]]")
# Review items created while a job runs, collected on the session by whoever inserts them.
CREATED_REVIEWS = "created_review_ids"


class Correction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="target_version 필드 이름, windows/<id>/<필드> 또는 benefits/<id>/<필드>")
    value: str | int | float | None
    quote: str = Field(description="고친 값의 근거가 되는 원문 표현")


class RevisionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(description="/application_windows/<window_key>/start|end, /title, /summary, /source_status_override")
    value: dict[str, str | None] | str = Field(description='기간이면 {"date": "YYYY-MM-DD", "time": "HH:MM" 또는 null}')
    quote: str = Field(description="새 값의 근거가 되는 정정 공고 원문 표현")


class RevisionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_opportunity_id: str
    kind: Literal["extension", "correction", "cancellation", "reopened"]
    intent_quote: str = Field(description="정정·연장임을 밝히는 원문 문장")
    patches: list[RevisionPatch]


class ReviewVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Literal["dismiss", "fix", "merge", "revise", "escalate"]
    confidence: Literal["high", "medium", "low"]
    reason: str = Field(max_length=2000)
    corrections: list[Correction] = Field(default_factory=list)
    same_opportunity_ids: list[str] = Field(default_factory=list)
    revisions: list[RevisionAction] = Field(default_factory=list)

    @property
    def dismissed(self) -> bool:
        return self.verdict == "dismiss" and self.confidence == "high"

    @property
    def acted(self) -> bool:
        """The model settles the item itself: high confidence and an action it may take."""
        return self.confidence == "high" and self.verdict in {"dismiss", "fix", "merge", "revise"}


class ActionRefused(ValueError):
    """The model's action does not fit the item; people decide instead."""


TEXT_COLUMNS = {"title", "summary", "provider_name", "administrator_name", "support_summary",
                "eligibility_summary", "application_instructions", "selection_process", "round_label"}
COLUMN_CHOICES = {"selection_capacity_scope": {"final_selection", "university_nomination"},
                  "academic_term": {"spring", "summer", "fall", "winter", "annual", "other", "unknown"}}
INTEGER_COLUMNS = {"selection_capacity", "academic_year"}
WINDOW_FIELDS = {"start_date": "date", "end_date": "date", "start_time": "time", "end_time": "time",
                 "raw_text": "text", "conditions_text": "text"}
BENEFIT_FIELDS = {"amount_kind": "amount_kind", "amount_min": "amount", "amount_max": "amount",
                  "raw_text": "text", "conditions_text": "text"}
AMOUNT_KINDS = {"fixed", "maximum", "range", "percentage", "formula", "variable", "unknown"}


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
            ON CONFLICT (crawl_run_id, stage, job_key) DO UPDATE  -- an item sent back for another look
              SET status='queued', attempt_count=0, available_at=now(), started_at=NULL,
                  finished_at=NULL, worker_id=NULL, error_code=NULL, error_message=NULL
              WHERE inha_policy.crawl_jobs.status NOT IN ('queued', 'retry', 'running')
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
            SELECT ov.opportunity_id, ov.title, ov.provider_name, ov.academic_year,
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
    context = {"review_kind": review["review_kind"], "issue": issue(review["review_kind"], payload),
               "details": details, "opportunities": opportunities, "extracted_item": extracted_item}
    if review["entity_type"] == "opportunity_version":
        context["target_version"] = await _editable(session, review["entity_id"])
    if review["review_kind"] == "revision_candidate":
        own, targets = await _revision_candidates(session, review)
        context["amendment_opportunities"] = own
        context["revision_targets"] = targets
    return context, notice_version_id


async def _opportunity_card(session: AsyncSession, opportunity_id: Any) -> dict[str, Any] | None:
    row = (await session.execute(text("""
        SELECT ov.opportunity_id, ov.title, ov.provider_name, ov.academic_year, ov.academic_term,
               ov.round_label, ov.publication_state,
               (SELECT string_agg(DISTINCT nv.title || ' (' || coalesce(nv.published_on::text, '') || ')', ' / ')
                  FROM inha_policy.opportunity_version_sources s
                  JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                 WHERE s.opportunity_version_id=ov.id) AS source_notices,
               (SELECT json_agg(json_build_object('window_key', w.window_key, 'kind', w.window_kind,
                                                  'start', w.start_date, 'start_time', w.start_time,
                                                  'end', w.end_date, 'end_time', w.end_time,
                                                  'text', w.raw_text) ORDER BY w.window_key)
                  FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id) AS windows
        FROM inha_policy.opportunity_versions ov
        JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
        WHERE ov.opportunity_id=:id AND o.lifecycle_status <> 'merged'
        ORDER BY ov.version_no DESC LIMIT 1
    """), {"id": opportunity_id})).mappings().one_or_none()
    return {key: _plain(value) for key, value in row.items()} if row else None


async def _revision_candidates(session: AsyncSession, review: dict[str, Any]
                               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The amendment notice's own opportunities, and earlier ones it may amend (by title)."""
    notice = (await session.execute(text(
        "SELECT notice_id, title FROM inha_policy.notice_versions WHERE id=:id"
    ), {"id": review["entity_id"]})).mappings().one()
    own_ids = (await session.execute(text("""
        SELECT DISTINCT ov.opportunity_id FROM inha_policy.opportunity_version_sources s
        JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
        JOIN inha_policy.opportunity_versions ov ON ov.id=s.opportunity_version_id
        WHERE nv.notice_id=:notice
    """), {"notice": notice["notice_id"]})).scalars().all()
    # Amendments usually repeat the original notice's title with a marker: match the title
    # without markers against opportunity names and against the titles of their source notices.
    title = AMENDMENT_MARKERS.sub(" ", notice["title"] or "")
    target_ids = (await session.execute(text("""
        SELECT id FROM (
          SELECT o.id, max(greatest(similarity(ov.title, :title), similarity(nv.title, :title))) AS score
          FROM inha_policy.opportunities o
          JOIN inha_policy.opportunity_versions ov ON ov.opportunity_id=o.id
          JOIN inha_policy.opportunity_version_sources s ON s.opportunity_version_id=ov.id
          JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
          WHERE o.lifecycle_status <> 'merged' AND o.id <> ALL(CAST(:own AS uuid[]))
          GROUP BY o.id) ranked
        ORDER BY score DESC LIMIT 12
    """), {"title": title, "own": [str(item) for item in own_ids]})).scalars().all()
    own = [card for item in own_ids if (card := await _opportunity_card(session, item))]
    targets = [card for item in target_ids if (card := await _opportunity_card(session, item))]
    return own, targets


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID | Decimal | date | time):
        return str(value)
    return value


async def _editable(session: AsyncSession, version_id: uuid.UUID) -> dict[str, Any]:
    """The draft's values the model may correct, with the ids of its windows and benefits."""
    columns = sorted(TEXT_COLUMNS | set(COLUMN_CHOICES) | INTEGER_COLUMNS)
    row = (await session.execute(text(f"""
        SELECT {", ".join(columns)} FROM inha_policy.opportunity_versions WHERE id=:id
    """), {"id": version_id})).mappings().one()
    windows = (await session.execute(text("""
        SELECT id, window_kind, phase_label, start_date, start_time, end_date, end_time, raw_text,
               conditions_text FROM inha_policy.application_windows WHERE opportunity_version_id=:id
    """), {"id": version_id})).mappings().all()
    benefits = (await session.execute(text("""
        SELECT id, benefit_kind, amount_kind, amount_min, amount_max, payment_frequency, raw_text,
               conditions_text FROM inha_policy.benefits WHERE opportunity_version_id=:id
    """), {"id": version_id})).mappings().all()
    return {**{key: _plain(value) for key, value in row.items()},
            "windows": [{key: _plain(value) for key, value in item.items()} for item in windows],
            "benefits": [{key: _plain(value) for key, value in item.items()} for item in benefits]}


def source_text(blocks: dict[str, str]) -> str:
    joined = "\n\n".join(value for value in blocks.values() if value)
    if len(joined) > SOURCE_CHAR_LIMIT:
        return joined[:SOURCE_CHAR_LIMIT] + "\n…(이하 생략)"
    return joined


async def apply(session: AsyncSession, review: dict[str, Any], verdict: ReviewVerdict, model: str) -> str:
    """Record the verdict and, when the model is sure, carry out its action and close the item."""
    record: dict[str, Any] = {"model": model, "verdict": verdict.verdict,
                              "confidence": verdict.confidence, "reason": verdict.reason}
    quality = review["review_kind"] == "other" and review["entity_type"] == "opportunity_version"
    identity = review["review_kind"] == "identity_uncertain"
    if not verdict.acted:
        await open_item(session, review["id"], record)
        return "escalated"
    try:
        async with session.begin_nested():
            if verdict.verdict == "fix":
                if not quality or not verdict.corrections:
                    raise ActionRefused("corrections apply to a quality item's draft only")
                record["corrections"] = await _correct(session, review["entity_id"], verdict.corrections)
            elif verdict.verdict == "merge":
                if not identity:
                    raise ActionRefused("merging applies to identity items only")
                record["merges"] = await _merge(session, set(related_opportunity_ids(review)),
                                                verdict.same_opportunity_ids, verdict.reason,
                                                review["payload"])
            elif verdict.verdict == "revise":
                if review["review_kind"] != "revision_candidate" or not verdict.revisions:
                    raise ActionRefused("revise applies to revision candidates with revisions")
                if len({item.target_opportunity_id for item in verdict.revisions}) != len(verdict.revisions):
                    raise ActionRefused("one revision per target opportunity")
                record["revisions"] = [await _revise(session, review, action, verdict.reason, model)
                                       for action in verdict.revisions]
                if len(verdict.same_opportunity_ids) > 1:
                    # The amendment's own copies merge into the target they amend.
                    own, _targets = await _revision_candidates(session, review)
                    own_ids = {item["opportunity_id"] for item in own}
                    targets = {item.target_opportunity_id for item in verdict.revisions}
                    group = [item for item in verdict.same_opportunity_ids if item in targets]
                    if len(group) != 1:
                        raise ActionRefused("same_opportunity_ids must hold exactly one revision target")
                    record["merges"] = await _merge(session, own_ids | set(group), verdict.same_opportunity_ids,
                                                    verdict.reason, review["payload"], winner_id=group[0])
            if quality:
                await _clear_quality(session, review)
            elif review["payload"].get("operation") == "cross_notice_candidates":
                merged = {item["loser"] for item in record.get("merges", [])} | {
                    item["winner"] for item in record.get("merges", [])}
                for candidate in review["payload"].get("candidates") or []:
                    if candidate.get("opportunity_id") not in merged or verdict.verdict != "merge":
                        await _reject_proposal(session, candidate.get("decision_id"), verdict.reason)
    except (ActionRefused, DBAPIError) as exc:
        # The action does not fit the item or the database's rules: people decide instead.
        record["action_refused"] = (str(exc) if isinstance(exc, ActionRefused)
                                    else str(getattr(exc, "orig", exc)).splitlines()[0][:500])
        await open_item(session, review["id"], record)
        return "escalated"
    outcome = {"dismiss": "dismissed", "fix": "corrected", "merge": "merged",
               "revise": "revised"}[verdict.verdict]
    note = f"AI 2차 검토({model}): {verdict.reason}"
    if record.get("corrections"):
        note += " / 수정: " + "; ".join(f"{item['path']}: {item['before']!r} → {item['after']!r}"
                                       for item in record["corrections"])
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status=:status, resolved_at=clock_timestamp(),
          updated_at=clock_timestamp(), resolution_note=:note,
          payload = payload || jsonb_build_object('ai_review', CAST(:record AS jsonb))
        WHERE id=:id AND status='ai_pending'
    """), {"id": review["id"], "status": "dismissed" if verdict.verdict == "dismiss" else "resolved",
             "note": note[:4000], "record": json.dumps(record, ensure_ascii=False, default=str)})
    await session.execute(text("""
        INSERT INTO inha_policy.audit_logs
          (actor_kind, actor_id, action, entity_type, entity_id, after_state, reason, automated)
        VALUES ('worker', 'ai_reviewer', :action, 'review_item', CAST(:id AS text),
                CAST(:record AS jsonb), :reason, true)
    """), {"id": review["id"], "action": f"review_{outcome}_by_ai",
             "record": json.dumps(record, ensure_ascii=False, default=str), "reason": verdict.reason})
    return outcome


async def _correct(session: AsyncSession, version_id: uuid.UUID,
                   corrections: list[Correction]) -> list[dict[str, Any]]:
    """Write corrected values into the unpublished draft; any invalid one refuses them all."""
    state = (await session.execute(text(
        "SELECT published_at FROM inha_policy.opportunity_versions WHERE id=:id FOR UPDATE"
    ), {"id": version_id})).mappings().one_or_none()
    if state is None or state["published_at"] is not None:
        raise ActionRefused("only an unpublished draft can be corrected")
    done = []
    window_changes: dict[uuid.UUID, dict[str, Any]] = {}
    benefit_changes: dict[uuid.UUID, dict[str, Any]] = {}
    for item in corrections:
        parts = item.path.strip("/").split("/")
        if len(parts) == 1:
            column = parts[0]
            value = _coerce_column(column, item.value)
            before = (await session.execute(text(
                f"SELECT {column} FROM inha_policy.opportunity_versions WHERE id=:id"
            ), {"id": version_id})).scalar_one()
            await session.execute(text(
                f"UPDATE inha_policy.opportunity_versions SET {column}=:value WHERE id=:id"
            ), {"id": version_id, "value": value})
        elif len(parts) == 2 and parts[0] in {"windows", "benefits"}:
            if item.value is not None:
                raise ActionRefused(f"{item.path}: a whole row can only be removed (value null)")
            table = "application_windows" if parts[0] == "windows" else "benefits"
            try:
                row_id = uuid.UUID(parts[1])
            except ValueError as exc:
                raise ActionRefused(f"{item.path}: unknown id") from exc
            removed = (await session.execute(text(f"""
                DELETE FROM inha_policy.{table} WHERE id=:row AND opportunity_version_id=:id RETURNING raw_text
            """), {"row": row_id, "id": version_id})).one_or_none()
            if removed is None:
                raise ActionRefused(f"{item.path}: not a row of this version")
            before, value = removed[0], None
            window_changes.pop(row_id, None)
            benefit_changes.pop(row_id, None)
        elif len(parts) == 3 and parts[0] in {"windows", "benefits"}:
            table, fields = (("application_windows", WINDOW_FIELDS) if parts[0] == "windows"
                             else ("benefits", BENEFIT_FIELDS))
            if parts[2] not in fields:
                raise ActionRefused(f"{item.path} is not correctable")
            value = _coerce(fields[parts[2]], item.value, item.path)
            try:
                row_id = uuid.UUID(parts[1])
            except ValueError as exc:
                raise ActionRefused(f"{item.path}: unknown id") from exc
            before = (await session.execute(text(f"""
                SELECT {parts[2]} FROM inha_policy.{table} WHERE id=:row AND opportunity_version_id=:id
            """), {"row": row_id, "id": version_id})).one_or_none()
            if before is None:
                raise ActionRefused(f"{item.path}: not a row of this version")
            before = before[0]
            # Rows are written once below, so a row never passes through a half-corrected state.
            changes = window_changes if table == "application_windows" else benefit_changes
            changes.setdefault(row_id, {})[parts[2]] = value
        else:
            raise ActionRefused(f"{item.path} is not correctable")
        done.append({"path": item.path, "before": _plain(before), "after": _plain(value), "quote": item.quote})
    for row_id, changes in window_changes.items():
        current = dict((await session.execute(text("""
            SELECT start_date, start_time, end_date, end_time FROM inha_policy.application_windows
            WHERE id=:row
        """), {"row": row_id})).mappings().one())
        current.update(changes)
        for side in ("start", "end"):
            day, moment = current[f"{side}_date"], current[f"{side}_time"]
            if day is None and moment is not None:
                raise ActionRefused(f"windows/{row_id}/{side}_time needs a {side}_date")
            # Precision follows the corrected date and time, as the assembler sets it.
            current[f"{side}_precision"] = ("unknown" if day is None else "date" if moment is None
                                            else "minute" if moment.second == 0 else "second")
        columns = sorted(set(changes) | {"start_precision", "end_precision"})
        await session.execute(text(f"""
            UPDATE inha_policy.application_windows SET {", ".join(f"{key}=:{key}" for key in columns)}
            WHERE id=:row
        """), {"row": row_id, **{key: current[key] for key in columns}})
    for row_id, changes in benefit_changes.items():
        current = dict((await session.execute(text("""
            SELECT amount_kind, amount_min, amount_max FROM inha_policy.benefits WHERE id=:row
        """), {"row": row_id})).mappings().one())
        current.update(changes)
        if current["amount_kind"] == "fixed":  # one number: a corrected bound moves the other
            amount = changes.get("amount_max", changes.get("amount_min", current["amount_max"]))
            current["amount_min"] = current["amount_max"] = amount
        columns = sorted(set(changes) | {"amount_min", "amount_max"})
        await session.execute(text(f"""
            UPDATE inha_policy.benefits SET {", ".join(f"{key}=:{key}" for key in columns)} WHERE id=:row
        """), {"row": row_id, **{key: current[key] for key in columns}})
    await session.execute(text("""
        UPDATE inha_policy.opportunity_versions
        SET quality_flags=array_append(quality_flags, 'ai_corrected') WHERE id=:id
    """), {"id": version_id})
    return done


def _coerce_column(column: str, value: Any) -> Any:
    if column in TEXT_COLUMNS:
        return _coerce("text", value, column)
    if column in INTEGER_COLUMNS:
        if value is None or (isinstance(value, int) and value >= 0) or (isinstance(value, str) and value.isdigit()):
            return None if value is None else int(value)
        raise ActionRefused(f"{column} needs a non-negative integer")
    if column in COLUMN_CHOICES:
        if value is None or value in COLUMN_CHOICES[column]:
            return value
        raise ActionRefused(f"{column} must be one of {sorted(COLUMN_CHOICES[column])}")
    raise ActionRefused(f"{column} is not correctable")


def _coerce(kind: str, value: Any, path: str) -> Any:
    if value is None:
        return None
    try:
        if kind == "text":
            return str(value)
        if kind == "date":
            return date.fromisoformat(str(value))
        if kind == "time":
            return time.fromisoformat(str(value))
        if kind == "amount_kind":
            if value not in AMOUNT_KINDS:
                raise ValueError(f"one of {sorted(AMOUNT_KINDS)}")
            return value
        if kind == "amount":
            amount = Decimal(re.sub(r"[,\s원]", "", str(value)))
            if amount < 0:
                raise ValueError("negative")
            return amount
    except (ValueError, InvalidOperation) as exc:
        raise ActionRefused(f"{path}: {value!r} is not a valid {kind}") from exc
    raise ActionRefused(f"{path}: unknown kind")


async def _merge(session: AsyncSession, allowed: set[str], same_ids: list[str], reason: str,
                 payload: dict[str, Any], winner_id: str | None = None) -> list[dict[str, str]]:
    """Merge the opportunities the model found to be one into ``winner_id`` or else the oldest
    published of them."""
    ids = list(dict.fromkeys(same_ids))
    if len(ids) < 2 or not set(ids) <= allowed:
        raise ActionRefused("a merge needs two or more of the opportunities under review")
    rows = (await session.execute(text("""
        SELECT o.id, o.lifecycle_status, o.created_at,
               EXISTS (SELECT 1 FROM inha_policy.opportunity_versions v
                       WHERE v.opportunity_id=o.id AND v.publication_state='published') AS published
        FROM inha_policy.opportunities o WHERE o.id = ANY(CAST(:ids AS uuid[])) FOR UPDATE
    """), {"ids": ids})).mappings().all()
    live = [row for row in rows if row["lifecycle_status"] != "merged"]
    if len(live) < 2:
        raise ActionRefused("fewer than two of those opportunities are still unmerged")
    winner = next((row for row in live if str(row["id"]) == winner_id), None) or \
        sorted(live, key=lambda row: (not row["published"], row["created_at"]))[0]
    proposals = {str(item.get("opportunity_id")): item.get("decision_id")
                 for item in payload.get("candidates") or []}
    merges = []
    for loser in live:
        if loser["id"] == winner["id"]:
            continue
        decision_id = uuid.uuid4()
        proposal = proposals.get(str(loser["id"])) or proposals.get(str(winner["id"]))
        await session.execute(text("""
            INSERT INTO inha_policy.identity_decisions
              (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
               supersedes_decision_id, identity_basis, same_program_verified, same_cycle_verified,
               same_scope_verified, amendment_kind, rule_version, decision_reason, actor_kind,
               actor_id, observed_at)
            VALUES (:id, 'merge', 'confirmed', :winner, :loser, CAST(:proposal AS uuid),
                    'verified_cycle_scope', true, true, true, 'none', 'ai-review-1.0', :reason,
                    'ai_review', 'ai_reviewer', clock_timestamp())
        """), {"id": decision_id, "winner": winner["id"], "loser": loser["id"],
                 "proposal": proposal, "reason": reason})
        await session.execute(text("""
            UPDATE inha_policy.opportunities SET lifecycle_status='merged', merged_into_id=:winner,
              last_identity_decision_id=:decision, updated_at=now() WHERE id=:loser
        """), {"winner": winner["id"], "decision": decision_id, "loser": loser["id"]})
        merges.append({"loser": str(loser["id"]), "winner": str(winner["id"]), "decision_id": str(decision_id)})
    return merges


async def open_item(session: AsyncSession, review_id: uuid.UUID, record: dict[str, Any]) -> None:
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status='open', updated_at=clock_timestamp(),
          payload = payload || jsonb_build_object('ai_review', CAST(:record AS jsonb))
        WHERE id=:id AND status='ai_pending'
    """), {"id": review_id, "record": json.dumps(record, ensure_ascii=False)})


async def _clear_quality(session: AsyncSession, review: dict[str, Any]) -> None:
    """The model found the flagged draft correct: give it the quality the flags withheld and
    re-run the embed step, which publishes it when auto-publishing is on. A version with
    conflicting values stays needs_review and is published as reviewed (ai_review_cleared)."""
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
    # Values the extraction marked as conflicting keep the version at needs_review: the database
    # only publishes such a version as a reviewed one, as a person publishing it would.
    conflicts = (await session.execute(text("""
        SELECT EXISTS (SELECT 1 FROM inha_policy.field_evidence e
                       WHERE e.opportunity_version_id=:id AND e.candidate_status='conflicting'
                         AND NOT EXISTS (SELECT 1 FROM inha_policy.field_resolutions r
                                         WHERE r.id=e.resolution_id
                                           AND r.status IN ('agreed', 'resolved_explicit_update')))
    """), {"id": source["id"]})).scalar_one()
    quality = "needs_review" if conflicts else "partial" if provenance else "complete"
    await session.execute(text("""
        UPDATE inha_policy.opportunity_versions
        SET data_quality_status=:quality,
            quality_flags=array_cat(quality_flags, CAST(:flags AS text[]))
        WHERE id=:id
    """), {"id": source["id"], "quality": quality, "flags": ["ai_review_cleared", *provenance]})
    if provenance:  # incomplete sources are not publishable without people, reviewed or not
        return
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:run, 'embed', :key, :notice, :version, CAST(:payload AS jsonb))
        ON CONFLICT DO NOTHING
    """), {"run": source["crawl_run_id"], "key": f"opportunity:{source['id']}:ai-review",
             "notice": source["notice_id"], "version": source["notice_version_id"],
             "payload": json.dumps({"opportunity_version_id": str(source["id"]), "ai_reviewed": True})})


async def _revise(session: AsyncSession, review: dict[str, Any], action: RevisionAction, reason: str,
                  model: str) -> dict[str, Any]:
    """Apply the amendment to an earlier opportunity through the revision service, with the
    model's quotes located in the amendment notice's blocks."""
    from .revisions import EvidenceInput, FieldPatch, RevisionError, RevisionService, VerifiedRevision
    _own, targets = await _revision_candidates(session, review)
    if action.target_opportunity_id not in {item["opportunity_id"] for item in targets}:
        raise ActionRefused("the target must be one of the revision_targets shown")
    payload = review["payload"]
    if not payload.get("extraction_run_id"):
        raise ActionRefused("the revision candidate has no extraction run")
    service = RevisionService(session)
    blocks = await service._blocks(review["entity_id"])

    def evidence(quote: str) -> EvidenceInput:
        wanted = " ".join(quote.split())
        for block_id, block in blocks.items():
            if wanted and wanted in " ".join((block["text"] or "").split()):
                return EvidenceInput(block_id, quote)
        raise ActionRefused(f"quote not found in the amendment notice: {quote[:80]!r}")

    revision = VerifiedRevision(
        opportunity_id=uuid.UUID(action.target_opportunity_id),
        amendment_notice_version_id=review["entity_id"],
        extraction_run_id=uuid.UUID(str(payload["extraction_run_id"])),
        extraction_item_path=str(payload.get("extraction_item_path") or "/revisions/0"),
        kind=action.kind, intent=evidence(action.intent_quote),
        patches=tuple(FieldPatch(item.field_path, item.value, evidence(item.quote)) for item in action.patches),
        same_cycle_verified=True, same_scope_verified=True, new_value_verified=True,
        reason=f"AI 2차 검토({model}): {reason}", actor_id="ai_reviewer", actor_kind="ai_review")
    try:
        version_id = await service.apply(revision)
    except RevisionError as exc:
        raise ActionRefused(f"revision refused: {exc}") from exc
    await session.execute(text(
        "UPDATE inha_policy.review_items SET opportunity_id=:opportunity WHERE id=:id"
    ), {"id": review["id"], "opportunity": revision.opportunity_id})
    return {"opportunity_id": action.target_opportunity_id, "opportunity_version_id": str(version_id),
            "kind": action.kind, "patches": [item.model_dump() for item in action.patches]}


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
