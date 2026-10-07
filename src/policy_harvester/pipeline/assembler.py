from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..ai.schema_v2 import ExtractionBundleV2, Fact, Opportunity
from .edition import compare, core_name, draft_signature, version_signature


# Reparse appends a new sealed attempt; only the newest attempt per origin feeds extraction.
LATEST_DOCUMENTS_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (asset_occurrence_id) id, status
    FROM inha_policy.documents
    WHERE notice_version_id=:version AND sealed_at IS NOT NULL
    ORDER BY asset_occurrence_id, finished_at DESC NULLS LAST, attempt_no DESC
)
"""


def normalize_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    value = re.sub(r"\[(?:연장|기간연장|수정|정정|변경|재공고)\]", "", value)
    value = re.sub(r"(?:기간\s*)?(?:연장|정정|수정|재공고)", "", value)
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def fact_value(fact: Fact[Any]) -> Any | None:
    return fact.value if fact.state in {"stated", "resolved_update"} else None


class OpportunityAssembler:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def assemble(self, *, bundle: ExtractionBundleV2, extraction_run_id: uuid.UUID,
                       notice_version_id: uuid.UUID, crawl_run_id: uuid.UUID,
                       evidence_warnings: list[str] | None = None,
                       forced_opportunity_id: uuid.UUID | None = None,
                       target_item_index: int | None = None,
                       identity_decision_id: uuid.UUID | None = None) -> list[uuid.UUID]:
        notice = (await self.session.execute(text("""
            SELECT nv.notice_id, nv.observed_at FROM inha_policy.notice_versions nv WHERE nv.id=:id
        """), {"id": notice_version_id})).mappings().one()
        block_rows = (await self.session.execute(text("""
            SELECT b.id, b.document_id, b.text_content FROM inha_policy.document_blocks b
            JOIN inha_policy.documents d ON d.id=b.document_id
            WHERE d.notice_version_id=:id
        """), {"id": notice_version_id})).mappings().all()
        block_documents = {str(row["id"]): row["document_id"] for row in block_rows}
        self.block_texts = {str(row["id"]): row["text_content"] or "" for row in block_rows}
        provenance_flags = await self._provenance_flags(notice_version_id)
        if target_item_index is not None and not 0 <= target_item_index < len(bundle.opportunities):
            raise ValueError("target extraction item index is out of range")
        if forced_opportunity_id is not None:
            exists = (await self.session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM inha_policy.opportunities WHERE id=:id)"
            ), {"id": forced_opportunity_id})).scalar_one()
            if not exists:
                raise ValueError("forced opportunity does not exist")
        version_ids: list[uuid.UUID] = []
        claimed: set[uuid.UUID] = set()
        for index, draft in enumerate(bundle.opportunities):
            if target_item_index is not None and index != target_item_index:
                continue
            comparisons: list[dict[str, Any]] = []
            if forced_opportunity_id is not None:
                opportunity_id = forced_opportunity_id
            else:
                opportunity_id, comparisons = await self._resolve_same_notice_opportunity(
                    notice["notice_id"], draft, len(bundle.opportunities), claimed)
            if opportunity_id is None:
                opportunity_id = uuid.uuid4()
                await self.session.execute(text(
                    "INSERT INTO inha_policy.opportunities (id, program_key) VALUES (:id, :key)"
                ), {"id": opportunity_id,
                     "key": core_name(fact_value(draft.name) or draft.local_key,
                                      fact_value(draft.organization)) or draft.local_key})
                edit_kind = "initial"
                # Opportunities another item of this same extraction already took are its
                # siblings (one notice announcing two scholarships), not candidates for this one.
                unclaimed = [item for item in comparisons if not item.get("claimed")]
                if unclaimed:
                    await self._review("identity_uncertain", "opportunity", opportunity_id,
                                       opportunity_id, {
                                           "operation": "same_notice_unmatched",
                                           "notice_version_id": str(notice_version_id),
                                           "extracted_name": fact_value(draft.name),
                                           "existing_opportunity_ids": [
                                               item["opportunity_id"] for item in unclaimed],
                                           "comparisons": unclaimed})
            else:
                edit_kind = "split" if forced_opportunity_id else "in_place_edit"
            claimed.add(opportunity_id)
            version_no = (await self.session.execute(text("""
                SELECT coalesce(max(version_no), 0)+1 FROM inha_policy.opportunity_versions
                WHERE opportunity_id=:id
            """), {"id": opportunity_id})).scalar_one()
            previous = (await self.session.execute(text("""
                SELECT id FROM inha_policy.opportunity_versions
                WHERE opportunity_id=:id ORDER BY version_no DESC LIMIT 1
            """), {"id": opportunity_id})).scalar_one_or_none()
            version_id = uuid.uuid4()
            title = fact_value(draft.name) or "제목 확인 필요"
            quality_flags = list(bundle.warnings)
            if any(self._contains_conflict(value) for value in draft.model_dump(mode="json").values()):
                quality_flags.append("field_conflict")
            if quality_flags or bundle.coverage != "complete":
                quality = "needs_review"
            elif provenance_flags:
                quality = "partial"
                quality_flags.extend(provenance_flags)
            else:
                quality = "complete"
            details = {
                "aliases": draft.aliases,
                "category": draft.category,
                "application_methods": [item.model_dump(mode="json") for item in draft.application_methods],
                "selection": draft.selection.model_dump(mode="json"),
                "local_key": draft.local_key,
                "summary_is_generated": True,
            }
            selected = fact_value(draft.selection.selection_count)
            nominated = fact_value(draft.selection.nomination_quota)
            capacity, capacity_scope = ((selected, "final_selection") if selected is not None else
                                        (nominated, "university_nomination") if nominated is not None
                                        else (None, None))
            support_summary = "; ".join(item.description for item in draft.benefits if item.description)
            residual = [str(fact_value(item)) for item in draft.eligibility.residual_conditions
                        if fact_value(item)]
            eligibility_summary = "\n".join(residual)
            await self.session.execute(text("""
                INSERT INTO inha_policy.opportunity_versions
                  (id, opportunity_id, version_no, supersedes_version_id,
                   identity_decision_id, edit_kind,
                   schema_version, title, summary, categories, provider_name, academic_year,
                   academic_term, round_label, required_documents, contacts, additional_details,
                   data_quality_status, quality_flags, extraction_coverage, observed_at,
                   selection_capacity, selection_capacity_scope, selection_process,
                   support_summary, eligibility_summary)
                VALUES (:id, :opportunity_id, :version_no, :previous, :decision, :edit_kind,
                        :schema_version, :title, :summary, :categories, :provider, :year,
                        :term, :round, CAST(:documents AS jsonb), CAST(:contacts AS jsonb),
                        CAST(:details AS jsonb), :quality, :flags, CAST(:coverage AS jsonb), :observed,
                        :capacity, :capacity_scope, :selection_process, :support_summary,
                        :eligibility_summary)
            """), {
                "id": version_id, "opportunity_id": opportunity_id, "version_no": version_no,
                "previous": previous, "decision": identity_decision_id,
                "edit_kind": edit_kind, "schema_version": bundle.schema_version,
                "title": title, "summary": draft.summary,
                "categories": [draft.category],
                "provider": fact_value(draft.organization), "year": fact_value(draft.academic_year),
                "term": self._term(fact_value(draft.semester)), "round": fact_value(draft.round_label),
                "documents": json.dumps([item.model_dump(mode="json")
                                           for item in draft.required_documents], ensure_ascii=False),
                "contacts": json.dumps([item.model_dump(mode="json") for item in draft.contacts],
                                         ensure_ascii=False),
                "details": json.dumps(details, ensure_ascii=False), "quality": quality,
                "flags": quality_flags, "coverage": json.dumps({"state": bundle.coverage}),
                "observed": notice["observed_at"],
                "capacity": capacity if isinstance(capacity, int) and capacity >= 0 else None,
                "capacity_scope": capacity_scope if isinstance(capacity, int) else None,
                "selection_process": fact_value(draft.selection.method),
                "support_summary": support_summary or None,
                "eligibility_summary": eligibility_summary or None,
            })
            await self.session.execute(text("""
                INSERT INTO inha_policy.opportunity_version_sources
                  (opportunity_version_id, notice_version_id, extraction_run_id,
                   extraction_item_path, relation_kind, identity_decision_id, dependency_reason)
                VALUES (:version_id, :notice_version_id, :run_id, :path, 'original', :decision,
                        'current extraction source')
            """), {"version_id": version_id, "notice_version_id": notice_version_id,
                     "run_id": extraction_run_id, "path": f"/opportunities/{index}",
                     "decision": identity_decision_id})
            await self._insert_windows(version_id, draft)
            await self._insert_benefits(version_id, draft)
            await self._insert_eligibility(version_id, draft)
            await self._insert_evidence(version_id, notice_version_id, extraction_run_id,
                                        draft, block_documents, f"/opportunities/{index}",
                                        evidence_warnings or [])
            await self.session.execute(text("""
                INSERT INTO inha_policy.audit_logs
                  (actor_kind, actor_id, action, entity_type, entity_id, before_state,
                   after_state, automated)
                VALUES ('worker', 'assembler', 'opportunity_version_created', 'opportunity',
                        CAST(:opportunity AS text),
                        jsonb_build_object('opportunity_version_id', CAST(:previous AS text)),
                        jsonb_build_object('opportunity_version_id', CAST(:version AS text),
                                           'version_no', CAST(:version_no AS integer),
                                           'quality', CAST(:quality AS text)), true)
            """), {"opportunity": opportunity_id, "previous": previous, "version": version_id,
                     "version_no": version_no, "quality": quality})
            if quality == "needs_review":
                await self._review("other", "opportunity_version", version_id, opportunity_id,
                                   {"quality_flags": quality_flags, "coverage": bundle.coverage})
            await self.session.execute(text("""
                INSERT INTO inha_policy.crawl_jobs
                  (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
                VALUES (:crawl_run_id, 'embed', :key, :notice_id, :notice_version_id,
                        CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING
            """), {"crawl_run_id": crawl_run_id, "key": f"opportunity:{version_id}",
                     "notice_id": notice["notice_id"], "notice_version_id": notice_version_id,
                     "payload": json.dumps({"opportunity_version_id": str(version_id)})})
            version_ids.append(version_id)

        for index, revision in enumerate(bundle.revisions):
            await self._review("revision_candidate", "notice_version", notice_version_id, None,
                               {**revision.model_dump(mode="json"),
                                "extraction_run_id": str(extraction_run_id),
                                "extraction_item_path": f"/revisions/{index}"})
        # The model's identity_candidates stay in the extraction output only. They mostly restate the
        # notice's own program; cross-notice candidates come from pipeline.identity instead.
        return version_ids

    async def _provenance_flags(self, notice_version_id: uuid.UUID) -> list[str]:
        """Mirror the publication guard: OCR/partial documents or incomplete assets cap quality."""
        partial = (await self.session.execute(text(LATEST_DOCUMENTS_SQL + """
            SELECT count(*) FROM latest WHERE status <> 'succeeded'
        """), {"version": notice_version_id})).scalar_one()
        assets = (await self.session.execute(text("""
            SELECT asset_collection_status FROM inha_policy.notice_versions WHERE id=:id
        """), {"id": notice_version_id})).scalar_one()
        flags = []
        if partial:
            flags.append("provenance_document_partial")
        if assets != "complete":
            flags.append("asset_collection_incomplete")
        return flags

    async def _resolve_same_notice_opportunity(
            self, notice_id: uuid.UUID, draft: Opportunity, item_count: int,
            claimed: set[uuid.UUID]) -> tuple[uuid.UUID | None, list[dict[str, Any]]]:
        """Match an extracted item to an opportunity already built from the same notice.

        Returns the match (or None) and the comparisons, which are kept on the review item
        when a new opportunity has to be created next to existing ones.
        """
        rows = (await self.session.execute(text("""
            SELECT DISTINCT ON (ov.opportunity_id) ov.id, ov.opportunity_id, ov.title,
                   ov.provider_name, ov.academic_year, ov.academic_term, ov.round_label
            FROM inha_policy.opportunity_version_sources ovs
            JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
            JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
            JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
            WHERE nv.notice_id=:notice_id AND o.lifecycle_status <> 'merged'
            ORDER BY ov.opportunity_id, ov.version_no DESC
        """), {"notice_id": notice_id})).mappings().all()
        if not rows:
            return None, []
        mine = draft_signature(draft, self._term)
        comparisons = []
        for row in rows:
            theirs = await version_signature(self.session, row)
            result = compare(mine, theirs)
            comparisons.append({"opportunity_id": str(row["opportunity_id"]), "title": row["title"],
                                "claimed": row["opportunity_id"] in claimed, **result.as_json()})
        open_rows = [(row, item) for row, item in zip(rows, comparisons) if not item["claimed"]]
        same = [row for row, item in open_rows if item["verdict"] == "same"]
        if len(same) == 1:
            return same[0]["opportunity_id"], comparisons
        if same:
            return None, comparisons
        if item_count != 1 or len(rows) != 1 or not open_rows:
            return None, comparisons
        # One item now and one item from the previous extraction of the same post is the same
        # slot even when the model words the name differently, unless the edition changed.
        if comparisons[0]["verdict"] == "different_edition":
            return None, comparisons
        previous_items = (await self.session.execute(text("""
            SELECT jsonb_array_length(er.parsed_output->'opportunities')
            FROM inha_policy.opportunity_version_sources ovs
            JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
            JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
            JOIN inha_policy.extraction_runs er ON er.id=ovs.extraction_run_id
            WHERE nv.notice_id=:notice_id AND ov.opportunity_id=:opportunity
            ORDER BY ov.version_no DESC LIMIT 1
        """), {"notice_id": notice_id, "opportunity": rows[0]["opportunity_id"]})).scalar_one_or_none()
        if previous_items != 1:
            return None, comparisons
        if comparisons[0]["verdict"] == "different":
            await self._review("identity_uncertain", "opportunity", rows[0]["opportunity_id"],
                               rows[0]["opportunity_id"], {
                                   "operation": "same_slot_renamed",
                                   "extracted_name": fact_value(draft.name),
                                   "previous_title": rows[0]["title"],
                                   "comparison": comparisons[0]})
        return rows[0]["opportunity_id"], comparisons

    async def _insert_windows(self, version_id: uuid.UUID, draft: Opportunity) -> None:
        for item in draft.application_windows:
            start, end = fact_value(item.start), fact_value(item.end)
            # Month-only dates have no column; they stay in raw_text from the quotes.
            start_precision = start.precision if start and start.date else "unknown"
            end_precision = end.precision if end and end.date else "unknown"
            quotes = [reference.quote for fact in (item.start, item.end) for reference in fact.evidence]
            await self.session.execute(text("""
                INSERT INTO inha_policy.application_windows
                  (opportunity_version_id, window_key, window_kind, phase_label,
                   application_authority, scope_key, channel, start_date, start_time,
                   start_precision, end_date, end_time, end_precision, timezone, closing_rule,
                   confirmation_status, raw_text)
                VALUES (:version, :key, :kind, :label, :authority, :scope, NULL,
                        :start_date, :start_time, :start_precision, :end_date, :end_time,
                        :end_precision, 'Asia/Seoul', :closing, :confirmation, :raw)
            """), {
                "version": version_id, "key": item.local_key, "kind": item.stage,
                "label": item.label, "authority": item.submit_to, "scope": item.local_key,
                "start_date": start.date if start_precision != "unknown" else None,
                "start_time": start.time if start_precision == "minute" else None,
                "start_precision": start_precision,
                "end_date": end.date if end_precision != "unknown" else None,
                "end_time": end.time if end_precision == "minute" else None,
                "end_precision": end_precision,
                "closing": {"first_come": "until_filled", "rolling": "rolling",
                            "fixed": "fixed" if end else "unknown"}.get(item.closing_rule, "unknown"),
                "confirmation": "conflicting" if item.end.state == "conflict" else
                                ("confirmed" if end else "unknown"),
                "raw": " / ".join(dict.fromkeys(quotes)) or None,
            })

    async def _insert_benefits(self, version_id: uuid.UUID, draft: Opportunity) -> None:
        for index, item in enumerate(draft.benefits):
            minimum, maximum = fact_value(item.amount_min), fact_value(item.amount_max)
            percentage = fact_value(item.tuition_percentage)
            if minimum is not None and maximum is not None and minimum == maximum:
                amount_kind = "fixed"
            elif minimum is not None and maximum is not None:
                amount_kind = "range"
            elif maximum is not None:
                amount_kind = "maximum"
            elif percentage is not None:
                amount_kind = "percentage"
            else:
                amount_kind = "unknown"
            if amount_kind == "unknown" and minimum is not None:
                minimum = None  # a bare minimum is not a supported amount shape
            await self.session.execute(text("""
                INSERT INTO inha_policy.benefits
                  (opportunity_version_id, benefit_key, benefit_kind, amount_kind,
                   amount_min, amount_max, currency, percentage, payment_frequency, raw_text)
                VALUES (:version, :key, :kind, :amount_kind, :minimum, :maximum, :currency,
                        :percentage, :frequency, :raw)
            """), {"version": version_id, "key": f"benefit-{index}",
                     "kind": self._benefit_kind(item.benefit_type), "amount_kind": amount_kind,
                     "minimum": minimum, "maximum": maximum,
                     "currency": "KRW" if minimum is not None or maximum is not None else None,
                     "percentage": percentage,
                     "frequency": {"monthly": "per_month"}.get(item.frequency, item.frequency),
                     "raw": " · ".join(filter(None, [item.description, item.duration]))})

    def _cited_text(self, value: Any, limit: int = 6000) -> str:
        """Source text of the blocks an extracted section cites, in first-cited order."""
        block_ids: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if isinstance(node.get("block_id"), str):
                    block_ids.append(node["block_id"])
                for child in node.values():
                    walk(child)
            elif isinstance(node, list):
                for child in node:
                    walk(child)
        walk(value.model_dump(mode="json"))
        return "\n\n".join(self.block_texts.get(block, "") for block in dict.fromkeys(block_ids))[:limit]

    async def _insert_eligibility(self, version_id: uuid.UUID, draft: Opportunity) -> None:
        value = draft.eligibility
        levels = fact_value(value.student_types) or []
        grades = fact_value(value.grades) or []
        gpa, scale = fact_value(value.gpa_min), fact_value(value.gpa_scale)
        income = fact_value(value.income_bracket_max)
        regions = fact_value(value.regions) or []
        schools = fact_value(value.schools) or []
        enrollments = fact_value(value.enrollment_states) or []
        # The structured fields hold only conditions common to every applicant (alternatives live in
        # rule_tree), so they are projected exactly even when a rule tree exists.
        gpa_exact = gpa is not None and scale is not None
        await self.session.execute(text("""
            INSERT INTO inha_policy.eligibility_profiles
              (opportunity_version_id, university_rule_status, eligible_universities,
               academic_level_rule_status, academic_levels, enrollment_rule_status,
               enrollment_states, grade_min, grade_max, gpa_rule_status,
               gpa_projection_is_exact, gpa_threshold, gpa_operator, gpa_scale, gpa_basis,
               income_rule_status, income_projection_is_exact, income_metric, income_max,
               residency_rule_status, residency_projection_is_exact, residency_subject,
               residency_region_codes, original_eligibility_text, residual_conditions_text,
               rules, machine_evaluation_supported)
            VALUES (:version, :school_status, :schools, :level_status, :levels,
                    :enrollment_status, :enrollments, :grade_min, :grade_max, :gpa_status,
                    :gpa_exact, :gpa, :gpa_operator, :scale, :gpa_basis, :income_status,
                    :income_exact, :income_metric, :income, :region_status, :region_exact,
                    :region_subject, :regions, :original, :residual, CAST(:rules AS jsonb), :supported)
        """), {
            "version": version_id, "school_status": self._rule_state(value.schools.state),
            "schools": schools, "level_status": self._rule_state(value.student_types.state),
            "levels": levels, "enrollment_status": self._rule_state(value.enrollment_states.state),
            "enrollments": enrollments, "grade_min": min(grades) if grades else None,
            "grade_max": max(grades) if grades else None,
            "gpa_status": self._rule_state(value.gpa_min.state),
            "gpa_exact": gpa_exact, "gpa": gpa if gpa_exact else None,
            "gpa_operator": ">=" if gpa_exact else None, "scale": scale if gpa_exact else None,
            "gpa_basis": "other" if gpa_exact else None,
            "income_status": self._rule_state(value.income_bracket_max.state),
            "income_exact": income is not None,
            "income_metric": "kosaf_support_bracket" if income is not None else None,
            "income": income,
            "region_status": self._rule_state(value.regions.state),
            "region_exact": bool(regions),
            # v2 regions are the applicant's or the guardian's registered residence.
            "region_subject": "self_or_parent" if regions else None, "regions": regions,
            "original": self._cited_text(value),
            "residual": "\n".join(str(fact_value(item) or "") for item in value.residual_conditions),
            "rules": json.dumps(value.rule_tree.model_dump(mode="json") if value.rule_tree
                                else {"status": "unknown"}, ensure_ascii=False),
            "supported": value.rule_tree is not None,
        })

    async def _insert_evidence(self, version_id: uuid.UUID, notice_version_id: uuid.UUID,
                               run_id: uuid.UUID, draft: Opportunity,
                               block_documents: dict[str, uuid.UUID], base_path: str,
                               evidence_warnings: list[str]) -> None:
        invalid_paths = {warning.split(":", 1)[0] for warning in evidence_warnings}

        async def insert(reference: dict[str, Any], path: str, value: Any,
                         state: str = "stated") -> None:
            document_id = block_documents.get(reference["block_id"])
            if document_id is None:
                return
            invalid = any(candidate == path or candidate.startswith(path + ":")
                          for candidate in invalid_paths)
            status = "conflicting" if state == "conflict" else "accepted"
            verification = ("pending" if invalid or reference["evidence_type"] == "inferred"
                            else "verified")
            if status == "accepted" and verification != "verified":
                status = "candidate"
            field_path = path.split("/evidence/", 1)[0]
            await self.session.execute(text("""
                INSERT INTO inha_policy.field_evidence
                  (opportunity_version_id, notice_version_id, extraction_run_id,
                   document_id, block_id, field_path, candidate_value, quote_text,
                   assertion_kind, candidate_status, verification_status)
                VALUES (:version, :notice, :run, :document, :block, :path,
                        CAST(:value AS jsonb), :quote, :kind, :status, :verification)
            """), {"version": version_id, "notice": notice_version_id, "run": run_id,
                     "document": document_id, "block": uuid.UUID(reference["block_id"]),
                     "path": field_path, "value": json.dumps(value, ensure_ascii=False),
                     "quote": reference["quote"], "kind": reference["evidence_type"],
                     "status": status, "verification": verification})

        async def walk(value: Any, path: str) -> None:
            if isinstance(value, dict):
                if {"value", "state", "evidence"}.issubset(value):
                    for index, reference in enumerate(value["evidence"]):
                        await insert(reference, f"{path}/evidence/{index}", value["value"],
                                     value["state"])
                elif isinstance(value.get("evidence"), list):
                    candidate = {key: child for key, child in value.items() if key != "evidence"}
                    for index, reference in enumerate(value["evidence"]):
                        await insert(reference, f"{path}/evidence/{index}", candidate)
                for key, child in value.items():
                    if key != "evidence":
                        await walk(child, f"{path}/{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    await walk(child, f"{path}/{index}")
        await walk(draft.model_dump(mode="json"), base_path)

    async def _review(self, kind: str, entity_type: str, entity_id: uuid.UUID,
                      opportunity_id: uuid.UUID | None, payload: dict[str, Any]) -> None:
        await self.session.execute(text("""
            INSERT INTO inha_policy.review_items
              (review_kind, entity_type, entity_id, opportunity_id, payload)
            VALUES (:kind, :entity_type, :entity_id, :opportunity_id, CAST(:payload AS jsonb))
        """), {"kind": kind, "entity_type": entity_type, "entity_id": entity_id,
                 "opportunity_id": opportunity_id,
                 "payload": json.dumps(payload, ensure_ascii=False, default=str)})

    @staticmethod
    def _contains_conflict(value: Any) -> bool:
        if isinstance(value, dict):
            return value.get("state") == "conflict" or any(
                OpportunityAssembler._contains_conflict(child) for child in value.values())
        if isinstance(value, list):
            return any(OpportunityAssembler._contains_conflict(child) for child in value)
        return False

    @staticmethod
    def _term(value: str | None) -> str | None:
        if not value:
            return None
        if value in {"spring", "fall", "summer", "winter", "annual"}:  # schema v2 enum
            return value
        if value in {"first_half", "second_half"}:
            # Scholarship "상반기/하반기" rounds line up with the first/second semester.
            return "spring" if value == "first_half" else "fall"
        lowered = value.lower()
        # Match the term number itself; a year such as "2021" must not decide the term.
        numbered = re.search(r"(?<!\d)([12])\s*(?:학기|semester|term)", lowered)
        if numbered:
            return "spring" if numbered.group(1) == "1" else "fall"
        for needle, term in (("봄", "spring"), ("가을", "fall"), ("여름", "summer"),
                             ("겨울", "winter"), ("연간", "annual"), ("spring", "spring"),
                             ("fall", "fall")):
            if needle in lowered:
                return term
        if re.fullmatch(r"\s*([12])\s*", lowered):
            return "spring" if lowered.strip() == "1" else "fall"
        return "other"

    @staticmethod
    def _benefit_kind(value: str) -> str:
        return {"housing": "service", "loan": "other"}.get(value, value)

    @staticmethod
    def _rule_state(state: str) -> str:
        if state == "explicitly_none":
            return "unrestricted"
        if state in {"stated", "resolved_update", "conflict"}:
            return "restricted"
        return "unknown"
