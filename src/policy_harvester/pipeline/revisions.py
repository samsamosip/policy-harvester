"""Apply a verified cross-notice amendment as a new immutable opportunity version.

The base version is never edited. Its rows are copied into a new draft version, the
patched fields are changed there, the evidence they replace is kept as ``superseded``, and
every patch gets a ``field_resolutions`` row computed by ``revision_resolver.resolve_field``.
Same-notice edits do not come through here; they are re-extracted as in-place versions.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from revision_resolver import Candidate, FieldScope, VerifiedAmendment, resolve_field

from .assembler import LATEST_DOCUMENTS_SQL

RULE_VERSION = "revision-apply-1.0"
KINDS = {"extension", "correction", "cancellation", "reopened"}
WINDOW_PATH = re.compile(r"^/application_windows/([^/]+)/(start|end)$")
SCALAR_PATHS = {"/title": "title", "/summary": "summary",
                "/source_status_override": "source_status_override"}
STATUS_OVERRIDES = {"cancelled", "suspended", "closed_by_source"}


class RevisionError(ValueError):
    """The amendment cannot be applied; ``status`` suggests the HTTP status for callers."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class EvidenceInput:
    block_id: uuid.UUID
    quote: str


@dataclass(frozen=True)
class FieldPatch:
    field_path: str
    value: Any
    evidence: EvidenceInput
    supersedes_evidence_ids: tuple[uuid.UUID, ...] | None = None
    scope_key: str = "main"


@dataclass(frozen=True)
class VerifiedRevision:
    opportunity_id: uuid.UUID
    amendment_notice_version_id: uuid.UUID
    extraction_run_id: uuid.UUID
    kind: str
    intent: EvidenceInput
    patches: tuple[FieldPatch, ...]
    same_cycle_verified: bool
    same_scope_verified: bool
    new_value_verified: bool
    reason: str
    actor_id: str
    extraction_item_path: str = "/revisions/0"
    review_id: uuid.UUID | None = None
    evidence_notes: dict[str, Any] = field(default_factory=dict)


def normalize_window_value(value: Any) -> dict[str, Any]:
    """Canonical JSON for a window bound: {"date", "time", "precision", "timezone"}."""
    if not isinstance(value, dict) or not value.get("date"):
        raise RevisionError("window bounds need an object with an ISO date")
    try:
        parsed_date = date.fromisoformat(str(value["date"]))
        parsed_time = time.fromisoformat(str(value["time"])) if value.get("time") else None
    except ValueError as exc:
        raise RevisionError(f"invalid window bound: {exc}") from exc
    precision = "date" if parsed_time is None else ("second" if parsed_time.second else "minute")
    return {"date": parsed_date.isoformat(),
            "time": parsed_time.isoformat() if parsed_time else None,
            "precision": precision, "timezone": str(value.get("timezone") or "Asia/Seoul")}


def _same_text(quote: str, block: str) -> bool:
    return " ".join(quote.split()) in " ".join(block.split())


class RevisionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def apply(self, revision: VerifiedRevision) -> uuid.UUID:
        if revision.kind not in KINDS:
            raise RevisionError(f"kind must be one of {sorted(KINDS)}")
        if not (revision.same_cycle_verified and revision.same_scope_verified
                and revision.new_value_verified):
            raise RevisionError("same cycle, same scope and the new value must all be verified")
        if not revision.patches:
            raise RevisionError("at least one patch is required")
        paths = [(patch.field_path, patch.scope_key) for patch in revision.patches]
        if len(paths) != len(set(paths)):
            raise RevisionError("each field/scope can be patched once per revision")

        opportunity = (await self.session.execute(text("""
            SELECT id, lifecycle_status FROM inha_policy.opportunities WHERE id=:id FOR UPDATE
        """), {"id": revision.opportunity_id})).mappings().one_or_none()
        if opportunity is None:
            raise RevisionError("opportunity not found", 404)
        if opportunity["lifecycle_status"] == "merged":
            raise RevisionError("apply amendments to the opportunity it was merged into", 409)
        base = (await self.session.execute(text("""
            SELECT * FROM inha_policy.opportunity_versions WHERE opportunity_id=:id
            ORDER BY version_no DESC LIMIT 1
        """), {"id": revision.opportunity_id})).mappings().one_or_none()
        if base is None:
            raise RevisionError("opportunity has no version to amend", 409)
        notice = await self._amendment_notice(revision, base["id"])
        blocks = await self._blocks(revision.amendment_notice_version_id)
        for evidence in (revision.intent, *(patch.evidence for patch in revision.patches)):
            block = blocks.get(evidence.block_id)
            if block is None:
                raise RevisionError(f"block {evidence.block_id} is not in the amendment's current documents")
            if not evidence.quote.strip() or not _same_text(evidence.quote, block["text"]):
                raise RevisionError(f"quote is absent from block {evidence.block_id}")

        patches = [self._normalized(patch) for patch in revision.patches]
        await self._check_targets(base["id"], patches)
        canonical = await self._canonical_evidence_paths(base["id"])
        base_evidence = (await self.session.execute(text("""
            SELECT * FROM inha_policy.field_evidence WHERE opportunity_version_id=:id
        """), {"id": base["id"]})).mappings().all()
        superseded: dict[uuid.UUID, tuple[FieldPatch, dict[str, Any]]] = {}
        for patch in patches:
            if patch.supersedes_evidence_ids is None:
                rows = [row for row in base_evidence
                        if canonical.get(row["field_path"], row["field_path"]) == patch.field_path
                        and row["candidate_status"] in {"accepted", "candidate", "conflicting"}]
            else:
                by_id = {row["id"]: row for row in base_evidence}
                missing = [item for item in patch.supersedes_evidence_ids if item not in by_id]
                if missing:
                    raise RevisionError(f"superseded evidence is not in the base version: {missing}")
                rows = [by_id[item] for item in patch.supersedes_evidence_ids]
            for row in rows:
                if row["id"] in superseded:
                    raise RevisionError(f"evidence {row['id']} would be superseded twice")
                superseded[row["id"]] = (patch, dict(row))

        decision_id = uuid.uuid4()
        version_id = uuid.uuid4()
        await self.session.execute(text("""
            INSERT INTO inha_policy.identity_decisions
              (id, decision_kind, decision_status, opportunity_id, notice_id,
               source_notice_version_id, identity_basis, same_program_verified,
               same_cycle_verified, same_scope_verified, amendment_kind, update_scope_mode,
               evidence, before_state, after_state, rule_version, decision_reason, actor_kind,
               actor_id, observed_at)
            VALUES (:id, 'link_notice', 'confirmed', :opportunity, :notice, :notice_version,
                    'human_verified', true, true, true, :kind, 'patch', CAST(:evidence AS jsonb),
                    jsonb_build_object('opportunity_version_id', CAST(:base AS text)),
                    jsonb_build_object('opportunity_version_id', CAST(:version AS text)),
                    :rule, :reason, 'human', :actor, :observed)
        """), {"id": decision_id, "opportunity": revision.opportunity_id,
                 "notice": notice["notice_id"], "notice_version": revision.amendment_notice_version_id,
                 "kind": revision.kind, "base": base["id"], "version": version_id,
                 "evidence": json.dumps([{"block_id": str(revision.intent.block_id),
                                          "quote": revision.intent.quote,
                                          **revision.evidence_notes}], ensure_ascii=False),
                 "rule": RULE_VERSION, "reason": revision.reason, "actor": revision.actor_id,
                 "observed": notice["observed_at"]})
        await self._copy_version(base, version_id, decision_id, revision.kind, notice["observed_at"])
        await self.session.execute(text("""
            INSERT INTO inha_policy.opportunity_version_sources
              (opportunity_version_id, notice_version_id, extraction_run_id, extraction_item_path,
               relation_kind, relationship_status, identity_decision_id, dependency_reason)
            VALUES (:version, :notice_version, :run, :path, :kind, 'confirmed', :decision,
                    'verified amendment')
        """), {"version": version_id, "notice_version": revision.amendment_notice_version_id,
                 "run": revision.extraction_run_id, "path": revision.extraction_item_path,
                 "kind": revision.kind, "decision": decision_id})

        intent_id = await self._insert_evidence(
            version_id, revision, blocks, revision.intent, "/revision", "main", {"kind": revision.kind})
        previous_resolutions = {
            (row["field_path"], row["scope_key"]): row["id"] for row in (await self.session.execute(text("""
                SELECT DISTINCT ON (field_path, scope_key) id, field_path, scope_key
                FROM inha_policy.field_resolutions WHERE opportunity_id=:id
                ORDER BY field_path, scope_key, decided_at DESC
            """), {"id": revision.opportunity_id})).mappings()}
        resolution_ids: dict[tuple[str, str], uuid.UUID] = {}
        for patch in patches:
            new_evidence_id = await self._insert_evidence(
                version_id, revision, blocks, patch.evidence, patch.field_path, patch.scope_key,
                patch.value)
            old = [row for owner, row in superseded.values() if owner is patch]
            scope = FieldScope(str(revision.opportunity_id), base["cycle_key"] or "unknown",
                               patch.field_path, patch.scope_key)
            candidates = [Candidate(f"old:{row['id']}", scope, row["candidate_value"],
                                    str(row["notice_version_id"]), str(row["notice_version_id"]),
                                    (str(row["id"]),)) for row in old]
            candidates.append(Candidate("new", scope, patch.value, str(notice["notice_id"]),
                                        str(revision.amendment_notice_version_id),
                                        (str(new_evidence_id),)))
            directive = VerifiedAmendment(
                "directive", scope, revision.kind, "new", tuple(item.key for item in candidates[:-1]), (str(intent_id),),
                True, True, True, True)
            outcome = resolve_field(candidates, [directive] if old else [])
            if outcome.status not in {"resolved_explicit_update", "agreed"}:
                raise RevisionError(f"{patch.field_path}: {'; '.join(outcome.reasons)}", 409)
            resolution_id = uuid.uuid4()
            resolution_ids[(patch.field_path, patch.scope_key)] = resolution_id
            explicit = outcome.status == "resolved_explicit_update"
            await self.session.execute(text("""
                INSERT INTO inha_policy.field_resolutions
                  (id, opportunity_id, opportunity_version_id, field_path, scope_key, scope,
                   status, selected_value, selected_evidence_id, intent_evidence_id,
                   supersedes_resolution_id, rule_version, precedence_basis,
                   same_cycle_verified, same_scope_verified, explicit_amendment_verified,
                   new_value_verified, amendment_kind, update_scope_mode, supersession_edges,
                   decision_reason, observed_at)
                VALUES (:id, :opportunity, :version, :path, :scope_key, CAST(:scope AS jsonb),
                        :status, CAST(:value AS jsonb), :selected, :intent, :previous, :rule,
                        :basis, true, true, true, true, :kind, 'patch', CAST(:edges AS jsonb),
                        :reason, :observed)
            """), {"id": resolution_id, "opportunity": revision.opportunity_id,
                     "version": version_id, "path": patch.field_path, "scope_key": patch.scope_key,
                     "scope": json.dumps({"cycle_key": scope.cycle_key, "scope_key": patch.scope_key}),
                     "status": outcome.status, "value": json.dumps(patch.value, ensure_ascii=False),
                     "selected": new_evidence_id, "intent": intent_id,
                     "previous": previous_resolutions.get((patch.field_path, patch.scope_key)),
                     "rule": outcome.rule_version,
                     "basis": "explicit_amendment" if explicit else "agreement",
                     "kind": revision.kind,
                     "edges": json.dumps([{"from": str(row["id"]), "to": str(new_evidence_id)}
                                          for row in old]),
                     "reason": "; ".join(outcome.reasons), "observed": notice["observed_at"]})
            await self._apply_patch(version_id, patch)

        await self._copy_evidence(base["id"], version_id, superseded, resolution_ids)
        await self.session.execute(text("""
            UPDATE inha_policy.opportunities SET last_identity_decision_id=:decision, updated_at=now()
            WHERE id=:id
        """), {"decision": decision_id, "id": revision.opportunity_id})
        await self.session.execute(text("""
            INSERT INTO inha_policy.crawl_jobs
              (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
            VALUES (:run, 'embed', :key, :notice, :notice_version, CAST(:payload AS jsonb))
        """), {"run": notice["crawl_run_id"], "key": f"opportunity:{version_id}",
                 "notice": notice["notice_id"], "notice_version": revision.amendment_notice_version_id,
                 "payload": json.dumps({"opportunity_version_id": str(version_id)})})
        if revision.review_id is not None:
            await self.session.execute(text("""
                UPDATE inha_policy.review_items SET status='resolved', resolution_note=:reason,
                  opportunity_id=:opportunity, resolved_at=now(), updated_at=now()
                WHERE id=:id AND status IN ('open', 'in_review')
            """), {"id": revision.review_id, "reason": revision.reason,
                     "opportunity": revision.opportunity_id})
        await self.session.execute(text("""
            INSERT INTO inha_policy.audit_logs
              (actor_kind, actor_id, action, entity_type, entity_id, before_state, after_state,
               reason, automated)
            VALUES ('admin_user', :actor, 'revision_applied', 'opportunity', :opportunity,
                    jsonb_build_object('opportunity_version_id', CAST(:base AS text)),
                    CAST(:after AS jsonb), :reason, false)
        """), {"actor": revision.actor_id, "opportunity": str(revision.opportunity_id),
                 "base": base["id"], "reason": revision.reason,
                 "after": json.dumps({"opportunity_version_id": str(version_id),
                                      "identity_decision_id": str(decision_id),
                                      "patches": [{"field_path": p.field_path, "value": p.value}
                                                  for p in patches]}, ensure_ascii=False)})
        return version_id

    async def _amendment_notice(self, revision: VerifiedRevision, base_id: uuid.UUID) -> dict[str, Any]:
        notice = (await self.session.execute(text("""
            SELECT nv.notice_id, nv.observed_at, nv.crawl_run_id, nv.sealed_at,
                   er.status AS run_status, er.sealed_at AS run_sealed
            FROM inha_policy.notice_versions nv
            LEFT JOIN inha_policy.extraction_runs er
              ON er.id=:run AND er.notice_version_id=nv.id
            WHERE nv.id=:id
        """), {"id": revision.amendment_notice_version_id,
                 "run": revision.extraction_run_id})).mappings().one_or_none()
        if notice is None:
            raise RevisionError("amendment notice version not found", 404)
        if notice["sealed_at"] is None:
            raise RevisionError("amendment notice version is not sealed", 409)
        if notice["run_status"] != "succeeded" or notice["run_sealed"] is None:
            raise RevisionError("extraction run must be a sealed successful run of the amendment notice", 409)
        same_notice = (await self.session.execute(text("""
            SELECT EXISTS (
              SELECT 1 FROM inha_policy.opportunity_version_sources s
              JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
              WHERE s.opportunity_version_id=:base AND nv.notice_id=:notice)
        """), {"base": base_id, "notice": notice["notice_id"]})).scalar_one()
        if same_notice:
            raise RevisionError("the amendment notice is already a source; re-extract it instead", 409)
        return dict(notice)

    async def _blocks(self, notice_version_id: uuid.UUID) -> dict[uuid.UUID, dict[str, Any]]:
        rows = (await self.session.execute(text(LATEST_DOCUMENTS_SQL + """
            SELECT b.id, b.document_id, b.text_content
            FROM latest d JOIN inha_policy.document_blocks b ON b.document_id=d.id
            WHERE d.status IN ('succeeded', 'partial')
        """), {"version": notice_version_id})).mappings().all()
        return {row["id"]: {"document_id": row["document_id"], "text": row["text_content"]}
                for row in rows}

    @staticmethod
    def _normalized(patch: FieldPatch) -> FieldPatch:
        if WINDOW_PATH.match(patch.field_path):
            value = normalize_window_value(patch.value)
        elif patch.field_path == "/source_status_override":
            if patch.value not in STATUS_OVERRIDES:
                raise RevisionError(f"status override must be one of {sorted(STATUS_OVERRIDES)}")
            value = patch.value
        elif patch.field_path in SCALAR_PATHS:
            if not isinstance(patch.value, str) or not patch.value.strip():
                raise RevisionError(f"{patch.field_path} needs a non-empty string")
            value = patch.value.strip()
        else:
            raise RevisionError(f"unsupported patch path {patch.field_path}")
        return FieldPatch(patch.field_path, value, patch.evidence, patch.supersedes_evidence_ids,
                          patch.scope_key)

    async def _check_targets(self, base_id: uuid.UUID, patches: list[FieldPatch]) -> None:
        keys = set((await self.session.execute(text("""
            SELECT window_key FROM inha_policy.application_windows WHERE opportunity_version_id=:id
        """), {"id": base_id})).scalars())
        for patch in patches:
            match = WINDOW_PATH.match(patch.field_path)
            if match and match.group(1) not in keys:
                raise RevisionError(f"window {match.group(1)} does not exist; known: {sorted(keys)}")

    async def _canonical_evidence_paths(self, base_id: uuid.UUID) -> dict[str, str]:
        """Map extraction-relative evidence paths to the canonical paths patches use."""
        rows = (await self.session.execute(text("""
            SELECT s.extraction_item_path, er.parsed_output
            FROM inha_policy.opportunity_version_sources s
            JOIN inha_policy.extraction_runs er ON er.id=s.extraction_run_id
            WHERE s.opportunity_version_id=:id AND s.extraction_item_path ~ '^/opportunities/[0-9]+$'
        """), {"id": base_id})).mappings().all()
        mapping: dict[str, str] = {}
        for row in rows:
            index = int(row["extraction_item_path"].rsplit("/", 1)[1])
            items = (row["parsed_output"] or {}).get("opportunities") or []
            if index >= len(items):
                continue
            base = row["extraction_item_path"]
            mapping[f"{base}/name"] = "/title"
            for position, window in enumerate(items[index].get("application_windows") or []):
                for bound in ("start", "end"):
                    mapping[f"{base}/application_windows/{position}/{bound}"] = (
                        f"/application_windows/{window['local_key']}/{bound}")
        return mapping

    async def _copy_version(self, base: dict[str, Any], version_id: uuid.UUID,
                            decision_id: uuid.UUID, kind: str, observed_at: Any) -> None:
        version_no = (await self.session.execute(text("""
            SELECT max(version_no)+1 FROM inha_policy.opportunity_versions WHERE opportunity_id=:id
        """), {"id": base["opportunity_id"]})).scalar_one()
        await self.session.execute(text("""
            INSERT INTO inha_policy.opportunity_versions
              (id, opportunity_id, version_no, supersedes_version_id, identity_decision_id,
               edit_kind, update_scope_mode, cycle_key, schema_version, title, summary,
               opportunity_kind, categories, tags, provider_name, administrator_name,
               academic_year, academic_term, round_label, support_summary, eligibility_summary,
               application_instructions, selection_process, selection_capacity,
               selection_capacity_scope, source_status_override, required_documents, contacts,
               links, additional_details, data_quality_status, quality_flags,
               unresolved_field_paths, extraction_coverage, observed_at)
            SELECT :id, opportunity_id, :version_no, id, :decision, :kind, 'patch', cycle_key,
                   schema_version, title, summary, opportunity_kind, categories, tags,
                   provider_name, administrator_name, academic_year, academic_term, round_label,
                   support_summary, eligibility_summary, application_instructions,
                   selection_process, selection_capacity, selection_capacity_scope,
                   source_status_override, required_documents, contacts, links,
                   additional_details, data_quality_status, quality_flags,
                   unresolved_field_paths, extraction_coverage, :observed
            FROM inha_policy.opportunity_versions WHERE id=:base
        """), {"id": version_id, "version_no": version_no, "decision": decision_id,
                 "kind": kind, "observed": observed_at, "base": base["id"]})
        for table, key in (("application_windows", "window_key"), ("benefits", "benefit_key")):
            columns = await self._columns(table, exclude={"id", "opportunity_version_id"})
            await self.session.execute(text(f"""
                INSERT INTO inha_policy.{table} (opportunity_version_id, {columns})
                SELECT :version, {columns} FROM inha_policy.{table}
                WHERE opportunity_version_id=:base ORDER BY {key}
            """), {"version": version_id, "base": base["id"]})
        columns = await self._columns("eligibility_profiles", exclude={"opportunity_version_id"})
        await self.session.execute(text(f"""
            INSERT INTO inha_policy.eligibility_profiles (opportunity_version_id, {columns})
            SELECT :version, {columns} FROM inha_policy.eligibility_profiles
            WHERE opportunity_version_id=:base
        """), {"version": version_id, "base": base["id"]})
        columns = await self._columns("opportunity_version_sources", exclude={"opportunity_version_id"})
        await self.session.execute(text(f"""
            INSERT INTO inha_policy.opportunity_version_sources (opportunity_version_id, {columns})
            SELECT :version, {columns} FROM inha_policy.opportunity_version_sources
            WHERE opportunity_version_id=:base
        """), {"version": version_id, "base": base["id"]})

    async def _columns(self, table: str, exclude: set[str]) -> str:
        names = (await self.session.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema='inha_policy' AND table_name=:table AND is_generated='NEVER'
            ORDER BY ordinal_position
        """), {"table": table})).scalars().all()
        return ", ".join(name for name in names if name not in exclude)

    async def _copy_evidence(self, base_id: uuid.UUID, version_id: uuid.UUID,
                             superseded: dict[uuid.UUID, tuple[FieldPatch, dict[str, Any]]],
                             resolution_ids: dict[tuple[str, str], uuid.UUID]) -> None:
        await self.session.execute(text("""
            INSERT INTO inha_policy.field_evidence
              (opportunity_version_id, notice_version_id, extraction_run_id, document_id,
               block_id, field_path, scope_key, candidate_value, quote_text, assertion_kind,
               candidate_status, verification_status, conflict_group_key, resolution_reason)
            SELECT :version, notice_version_id, extraction_run_id, document_id, block_id,
                   field_path, scope_key, candidate_value, quote_text, assertion_kind,
                   candidate_status, verification_status, conflict_group_key, resolution_reason
            FROM inha_policy.field_evidence
            WHERE opportunity_version_id=:base AND NOT (id = ANY(:superseded))
        """), {"version": version_id, "base": base_id, "superseded": list(superseded)})
        for evidence_id, (patch, row) in superseded.items():
            # The copy carries the canonical path so it can point at its resolution.
            await self.session.execute(text("""
                INSERT INTO inha_policy.field_evidence
                  (opportunity_version_id, notice_version_id, extraction_run_id, document_id,
                   block_id, field_path, scope_key, candidate_value, quote_text, assertion_kind,
                   candidate_status, verification_status, resolution_id, resolution_reason)
                SELECT :version, notice_version_id, extraction_run_id, document_id, block_id,
                       :path, :scope_key, candidate_value, quote_text, assertion_kind,
                       'superseded', verification_status, :resolution, :reason
                FROM inha_policy.field_evidence WHERE id=:id
            """), {"version": version_id, "path": patch.field_path, "scope_key": patch.scope_key,
                     "resolution": resolution_ids[(patch.field_path, patch.scope_key)],
                     "reason": f"superseded; original path {row['field_path']}", "id": evidence_id})

    async def _insert_evidence(self, version_id: uuid.UUID, revision: VerifiedRevision,
                               blocks: dict[uuid.UUID, dict[str, Any]], evidence: EvidenceInput,
                               path: str, scope_key: str, value: Any) -> uuid.UUID:
        evidence_id = uuid.uuid4()
        await self.session.execute(text("""
            INSERT INTO inha_policy.field_evidence
              (id, opportunity_version_id, notice_version_id, extraction_run_id, document_id,
               block_id, field_path, scope_key, candidate_value, quote_text, assertion_kind,
               candidate_status, verification_status)
            VALUES (:id, :version, :notice_version, :run, :document, :block, :path, :scope_key,
                    CAST(:value AS jsonb), :quote, 'explicit_correction', 'accepted', 'verified')
        """), {"id": evidence_id, "version": version_id,
                 "notice_version": revision.amendment_notice_version_id,
                 "run": revision.extraction_run_id,
                 "document": blocks[evidence.block_id]["document_id"], "block": evidence.block_id,
                 "path": path, "scope_key": scope_key,
                 "value": json.dumps(value, ensure_ascii=False), "quote": evidence.quote})
        return evidence_id

    async def _apply_patch(self, version_id: uuid.UUID, patch: FieldPatch) -> None:
        match = WINDOW_PATH.match(patch.field_path)
        if match:
            bound = match.group(2)
            value = patch.value
            await self.session.execute(text(f"""
                UPDATE inha_policy.application_windows
                SET {bound}_date=:date, {bound}_time=:time, {bound}_precision=:precision,
                    timezone=:timezone, confirmation_status='confirmed',
                    closing_rule=CASE WHEN :bound='end' AND closing_rule='unknown'
                                      THEN 'fixed' ELSE closing_rule END
                WHERE opportunity_version_id=:version AND window_key=:key
            """), {"date": date.fromisoformat(value["date"]),
                     "time": time.fromisoformat(value["time"]) if value["time"] else None,
                     "precision": value["precision"], "timezone": value["timezone"],
                     "bound": bound, "version": version_id, "key": match.group(1)})
            return
        column = SCALAR_PATHS[patch.field_path]
        await self.session.execute(text(f"""
            UPDATE inha_policy.opportunity_versions SET {column}=:value WHERE id=:id
        """), {"value": patch.value, "id": version_id})
