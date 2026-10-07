from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import signal
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from .ai.prompts import (EXTRACTION_PROMPT_VERSION_V2, EXTRACTION_SCHEMA_VERSION_V2,
                         EXTRACTION_SYSTEM_PROMPT_V2, MERGE_PROMPT_VERSION, MERGE_SYSTEM_PROMPT)
from .ai.providers import (EXCHANGE_LOG, ExtractionTarget, GenerationResult, ProviderRegistry,
                           TransientProviderError, Usage, is_transient, start_exchange_log,
                           validate_lenient_json)
from .ai.schema import validate_evidence
from .ai.schema_v2 import ExtractionBundleV2, restore_block_ids, short_block_ids, with_title_revision
from .config import effective_configuration, get_settings, resolved_settings
from .crawling.service import CrawlService
from .db import SessionFactory
from .documents import DetectedType, ParserRegistry, detect_type
from .documents.parsers import Block, ParseResult
from .documents.vision import (VISION_PAGE_PROMPT_VERSION, VISION_PROMPT, VISION_PROMPT_VERSION,
                               merge_transcriptions, page_prompt, prepare_image)
from .pipeline import LATEST_DOCUMENTS_SQL, OpportunityAssembler
from .pipeline.identity import IdentityCandidateService
from .ai.exchanges import persist_exchanges
from .pipeline.embeddings import activate_if_complete, ensure_profile
from .storage import build_object_store

logger = logging.getLogger(__name__)
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


STAGES = ("parse_document", "structure", "embed")
BOILERPLATE_NOTICE_COUNT = 5
HEARTBEAT_SECONDS = 60
TRANSCRIPTION_CONCURRENCY = 4
# Inputs this long are where single models dropped tracks in evaluation (about 11% of notices).
CROSSCHECK_MIN_INPUT_CHARS = 20_000


@dataclass(frozen=True)
class ExtractionOutcome:
    run_id: uuid.UUID
    bundle: ExtractionBundleV2
    validation_errors: list[str]
    model: str
    reused: bool = False  # taken from a sealed run of the same input, no model call


def extraction_differences(first: ExtractionBundleV2, second: ExtractionBundleV2) -> list[str]:
    """What two results of the same notice disagree on, among the values that matter most."""
    def stated(fact: Any) -> Any:
        return fact.value if fact.state in {"stated", "resolved_update"} else None

    def signature(bundle: ExtractionBundleV2) -> dict[str, Any]:
        items = bundle.opportunities
        return {
            "opportunities": len(items),
            "deadlines": sorted({str(stated(window.end).date) for item in items for window in item.application_windows
                                 if window.stage in {"application", "additional_application"}
                                 and stated(window.end) and stated(window.end).date}),
            "amounts": sorted({value for item in items for benefit in item.benefits
                               for value in (stated(benefit.amount_min), stated(benefit.amount_max))
                               if value is not None}),
            "headcounts": sorted({stated(item.selection.selection_count) for item in items
                                  if stated(item.selection.selection_count) is not None}),
        }
    left, right = signature(first), signature(second)
    return [key for key in left if left[key] != right[key]]


def retry_delay(exc: BaseException, attempt: int) -> timedelta:
    """Provider outages (overload, rate limits) last minutes, so they wait 10, 30, 90 minutes;
    other failures retry after 2, 4, 8 minutes."""
    if is_transient(exc):
        return timedelta(minutes=10 * 3 ** max(0, attempt - 1))
    return timedelta(minutes=2 ** attempt)


def needs_crosscheck(blocks: dict[str, str], bundle: ExtractionBundleV2) -> bool:
    """A second model checks results that are empty or multi-track, or come from a long notice."""
    return (not bundle.opportunities or bundle.coverage == "failed" or len(bundle.opportunities) >= 2
            or sum(len(value) for value in blocks.values()) >= CROSSCHECK_MIN_INPUT_CHARS)


def with_title_block(result: ParseResult, title: str) -> ParseResult:
    """Put the board title first: markers such as "[기간연장]" often appear only there, and
    extraction evidence must be able to quote them from a block."""
    title_block = Block("title:000000", "heading", title.strip(), source_path="notice/title",
                        metadata={"notice_title": True})
    blocks = (title_block, *result.blocks)
    return ParseResult(result.parser_name, result.parser_version, result.status, blocks,
                       "\n\n".join(block.text for block in blocks if block.text),
                       result.quality_flags, result.images)


def _db_text(value: str) -> str:
    # PostgreSQL text and jsonb cannot hold NUL; some PDFs embed it between glyphs.
    return value.replace("\x00", "")


def _db_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False).replace("\\u0000", "")


async def extraction_input(session: AsyncSession, notice_version_id: uuid.UUID
                           ) -> tuple[dict[str, str], list[uuid.UUID], dict[str, Any], str]:
    """Blocks of the newest parse attempts and the input hash that keys extraction reuse.

    Inline images repeated across many notices (site banners, social links) are left out;
    their ids are still recorded in the manifest.
    """
    boilerplate = (await session.execute(text(LATEST_DOCUMENTS_SQL + """
        SELECT l.id FROM latest l
        JOIN inha_policy.documents d ON d.id=l.id
        JOIN inha_policy.notice_version_assets nva ON nva.id=d.asset_occurrence_id
        WHERE nva.role='inline_image' AND nva.binary_asset_id IN (
            SELECT other.binary_asset_id FROM inha_policy.notice_version_assets other
            JOIN inha_policy.notice_versions nv ON nv.id=other.notice_version_id
            WHERE other.binary_asset_id IS NOT NULL
            GROUP BY other.binary_asset_id
            HAVING count(DISTINCT nv.notice_id) >= :repeats)
    """), {"version": notice_version_id, "repeats": BOILERPLATE_NOTICE_COUNT})).scalars().all()
    skipped = [str(item) for item in boilerplate]
    rows = (await session.execute(text(LATEST_DOCUMENTS_SQL + """
        SELECT b.id, b.text_content, d.id AS document_id, d.status
        FROM latest d JOIN inha_policy.document_blocks b ON b.document_id=d.id
        WHERE d.status IN ('succeeded', 'partial') AND NOT (d.id::text = ANY(:skipped))
        ORDER BY d.id, b.block_index
    """), {"version": notice_version_id, "skipped": skipped})).mappings().all()
    blocks = {str(row["id"]): row["text_content"] for row in rows if row["text_content"].strip()}
    missing = list((await session.execute(text(LATEST_DOCUMENTS_SQL + """
        SELECT id FROM latest WHERE status NOT IN ('succeeded', 'partial')
          AND NOT (id::text = ANY(:skipped))
    """), {"version": notice_version_id, "skipped": skipped})).scalars().all())
    manifest = {"blocks": [{"id": key, "sha256": hashlib.sha256(value.encode()).hexdigest()}
                            for key, value in blocks.items()],
                "missing_document_ids": [str(item) for item in missing],
                "boilerplate_document_ids": sorted(skipped)}
    input_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return blocks, missing, manifest, input_hash


class Worker:
    def __init__(self, stages: tuple[str, ...] = STAGES):
        self.stages = stages
        # Repeated pictures (logos, banners, shared posters) are transcribed once per process.
        self.transcription_cache: dict[tuple[str, str, str], str] = {}
        self.settings = get_settings()
        self.store = build_object_store(self.settings)
        self.parsers = ParserRegistry()
        self.providers = ProviderRegistry(self.settings)

    async def run_once(self) -> bool:
        async with SessionFactory() as session:
            job = await self._claim(session)
            if job is None:
                return False
            exchange_log = start_exchange_log()
            self.current_extraction_run_id = None
            heartbeat = asyncio.create_task(self._heartbeat(job["id"]))
            try:
                return await self._run_job(session, job)
            finally:
                heartbeat.cancel()
                if exchange_log:
                    await self._persist_exchanges(job, exchange_log)

    async def _heartbeat(self, job_id: uuid.UUID) -> None:
        """Keep a running job's lock fresh. Jobs can outlast the stale-lock timeout (a long
        extraction plus its cross-check), and with several workers a stale-looking job would be
        claimed and run twice."""
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with SessionFactory() as session:
                    await session.execute(text("""
                        UPDATE inha_policy.crawl_jobs SET heartbeat_at=now()
                        WHERE id=:id AND status='running'
                    """), {"id": job_id})
                    await session.commit()
            except Exception:  # a missed beat is retried next minute
                logger.warning("heartbeat update failed for job %s", job_id, exc_info=True)

    async def _persist_exchanges(self, job: dict[str, Any], log: list[dict[str, Any]]) -> None:
        """Raw request/response pairs are kept even when the job itself failed."""
        try:
            async with SessionFactory() as session:
                await persist_exchanges(session, self.store, log,
                                        notice_version_id=job.get("notice_version_id"),
                                        extraction_run_id=self.current_extraction_run_id)
                await session.commit()
        except Exception:
            logger.exception("could not persist llm exchanges", extra={"job_id": str(job["id"])})

    async def _run_job(self, session: AsyncSession, job: dict[str, Any]) -> bool:
        try:
            if job["stage"] == "parse_document":
                await self._parse_document(session, job)
            elif job["stage"] == "structure":
                await self._structure(session, job)
            elif job["stage"] == "embed":
                await self._embed(session, job)
            else:
                raise ValueError(f"worker stage is not implemented: {job['stage']}")
            await session.execute(text("""
                UPDATE inha_policy.crawl_jobs SET status='succeeded', finished_at=now(),
                  heartbeat_at=now() WHERE id=:id
            """), {"id": job["id"]})
            if job["stage"] == "parse_document":
                await self._enqueue_structure_if_ready(session, job)
            await session.commit()
        except asyncio.CancelledError:
            await session.rollback()
            await session.execute(text("""
                UPDATE inha_policy.crawl_jobs SET status='retry', available_at=now(),
                  locked_at=NULL, heartbeat_at=now(), worker_id=NULL,
                  error_code='CancelledError', error_message='worker interrupted'
                WHERE id=:id
            """), {"id": job["id"]})
            if job["stage"] == "structure":
                await session.execute(text("""
                    UPDATE inha_policy.extraction_runs SET status='failed',
                      error_code='CancelledError', error_message='worker interrupted',
                      finished_at=now(), sealed_at=now()
                    WHERE notice_version_id=:version AND status='running'
                """), {"version": job["notice_version_id"]})
            await session.commit()
            raise
        except Exception as exc:
            logger.exception("job failed", extra={"job_id": str(job["id"]), "stage": job["stage"]})
            await session.rollback()
            if job["stage"] == "structure":
                await session.execute(text("""
                    UPDATE inha_policy.extraction_runs SET status='failed',
                      error_code=:code, error_message=:message,
                      finished_at=now(), sealed_at=now()
                    WHERE notice_version_id=:version AND status='running'
                """), {"code": exc.__class__.__name__, "message": str(exc)[:4000],
                         "version": job["notice_version_id"]})
            await self._fail_job(session, job, exc)
            await session.commit()
        return True

    async def _claim(self, session: AsyncSession) -> dict[str, Any] | None:
        await session.execute(text("""
            UPDATE inha_policy.extraction_runs er SET status='failed',
              error_code='StaleLock', error_message='worker heartbeat timeout',
              finished_at=now(), sealed_at=now()
            FROM inha_policy.crawl_jobs j
            WHERE j.stage='structure' AND j.status='running'
              AND j.heartbeat_at < now() - interval '15 minutes'
              AND er.notice_version_id=j.notice_version_id
              AND er.status='running' AND er.sealed_at IS NULL
        """))
        await session.execute(text("""
            UPDATE inha_policy.crawl_jobs SET status='retry', available_at=now(),
              worker_id=NULL, locked_at=NULL, error_code='StaleLock',
              error_message='recovered after worker heartbeat timeout'
            WHERE status='running' AND heartbeat_at < now() - interval '15 minutes'
        """))
        row = (await session.execute(text("""
            WITH selected AS (
              SELECT id FROM inha_policy.crawl_jobs
              WHERE status IN ('queued', 'retry') AND available_at <= now()
                AND stage = ANY(:stages)
              ORDER BY available_at, created_at FOR UPDATE SKIP LOCKED LIMIT 1
            )
            UPDATE inha_policy.crawl_jobs j SET status='running', worker_id=:worker,
              locked_at=now(), heartbeat_at=now(), started_at=coalesce(started_at, now()),
              attempt_count=attempt_count+1
            FROM selected WHERE j.id=selected.id RETURNING j.*
        """), {"worker": WORKER_ID, "stages": list(self.stages)})).mappings().one_or_none()
        await session.commit()
        return dict(row) if row else None

    async def _fail_job(self, session: AsyncSession, job: dict[str, Any], exc: Exception) -> None:
        retry = job["attempt_count"] < job["max_attempts"]
        await session.execute(text("""
            UPDATE inha_policy.crawl_jobs SET status=:status, available_at=:available,
              finished_at=CASE WHEN :retry THEN NULL ELSE now() END,
              error_code=:code, error_message=:message WHERE id=:id
        """), {"status": "retry" if retry else "failed",
                 "available": datetime.now(UTC) + retry_delay(exc, job["attempt_count"]),
                 "retry": retry, "code": exc.__class__.__name__, "message": str(exc)[:4000],
                 "id": job["id"]})
        if not retry:
            kind = {"parse_document": "parsing_failed", "structure": "llm_validation_failed",
                    "embed": "embedding_failed"}[job["stage"]]
            metric_key = {"parse_document": "parser_errors", "structure": "ai_errors",
                          "embed": "embedding_errors"}[job["stage"]]
            await session.execute(text("""
                UPDATE inha_policy.crawl_runs
                SET metrics = metrics || jsonb_build_object(
                    CAST(:metric_key AS text),
                    coalesce((metrics ->> CAST(:metric_key AS text))::integer, 0) + 1)
                WHERE id=:run_id
            """), {"metric_key": metric_key, "run_id": job["crawl_run_id"]})
            entity_id = job["notice_version_id"] or job["notice_id"]
            await session.execute(text("""
                INSERT INTO inha_policy.review_items
                  (review_kind, entity_type, entity_id, payload)
                VALUES (:kind, 'crawl_job', :entity_id, CAST(:payload AS jsonb))
            """), {"kind": kind, "entity_id": entity_id,
                     "payload": json.dumps({"job_id": str(job["id"]), "error": str(exc)},
                                           ensure_ascii=False)})

    async def _parse_document(self, session: AsyncSession, job: dict[str, Any]) -> None:
        payload = job["payload"]
        origin = payload["origin"]
        occurrence_id = uuid.UUID(payload["asset_occurrence_id"]) if origin == "asset" else None
        if origin == "html_body":
            source = (await session.execute(text("""
                SELECT body_html, title FROM inha_policy.notice_versions WHERE id=:id
            """), {"id": job["notice_version_id"]})).mappings().one()
            raw = source["body_html"].encode("utf-8")
            filename = f"{source['title']}.html"
        else:
            source = (await session.execute(text("""
                SELECT b.storage_key, nva.original_filename, nva.binary_asset_id
                FROM inha_policy.notice_version_assets nva
                JOIN inha_policy.binary_assets b ON b.id=nva.binary_asset_id
                WHERE nva.id=:id
            """), {"id": occurrence_id})).mappings().one()
            raw = self.store.get(source["storage_key"])
            filename = source["original_filename"]
        # Stored bodies are DOM fragments (often starting with <p>), so sniffing cannot be trusted.
        detected = (DetectedType("html", "text/html", ".html", "high") if origin == "html_body"
                    else detect_type(raw, filename))
        config = await effective_configuration(session)
        vision_model = config["llm.model"]["value"]
        config_hash = hashlib.sha256(json.dumps(
            {"detected": detected.format, "vision_model": vision_model,
             "vision_prompt": VISION_PROMPT_VERSION, "page_prompt": VISION_PAGE_PROMPT_VERSION},
            sort_keys=True).encode()).hexdigest()
        parser_name, parser_version = self.parsers.identity(detected)
        if origin == "asset":
            cached = (await session.execute(text("""
                SELECT d.id, d.status, d.parsed_text, d.parsed_markdown, d.structure_storage_key,
                       d.language_code, d.quality_flags
                FROM inha_policy.documents d
                JOIN inha_policy.notice_version_assets cached_occurrence
                  ON cached_occurrence.id=d.asset_occurrence_id
                WHERE cached_occurrence.binary_asset_id=:binary_asset
                  AND d.parser_name=:parser_name AND d.parser_version=:parser_version
                  AND d.parser_config_sha256=:config AND d.normalizer_version='blocks-1.0'
                  AND d.sealed_at IS NOT NULL AND d.status IN ('succeeded','partial')
                ORDER BY d.finished_at DESC LIMIT 1
            """), {"binary_asset": source["binary_asset_id"], "parser_name": parser_name,
                     "parser_version": parser_version, "config": config_hash})).mappings().one_or_none()
            if cached:
                attempt = (await session.execute(text("""
                    SELECT coalesce(max(attempt_no), 0)+1 FROM inha_policy.documents
                    WHERE notice_version_id=:version AND asset_occurrence_id=:asset
                      AND parser_name=:name AND parser_version=:parser_version
                      AND parser_config_sha256=:config AND normalizer_version='blocks-1.0'
                """), {"version": job["notice_version_id"], "asset": occurrence_id,
                         "name": parser_name, "parser_version": parser_version,
                         "config": config_hash})).scalar_one()
                document_id = uuid.uuid4()
                await session.execute(text("""
                    INSERT INTO inha_policy.documents
                      (id, notice_version_id, asset_occurrence_id, origin_kind, parser_name,
                       parser_version, parser_config_sha256, normalizer_version, attempt_no,
                       status, parsed_text, parsed_markdown, structure_storage_key, language_code,
                       quality_flags, started_at, finished_at)
                    VALUES (:id, :version, :asset, 'asset', :name, :parser_version, :config,
                            'blocks-1.0', :attempt, :status, :text, :markdown, :structure,
                            :language, :flags, now(), now())
                """), {"id": document_id, "version": job["notice_version_id"],
                         "asset": occurrence_id, "name": parser_name,
                         "parser_version": parser_version, "config": config_hash,
                         "attempt": attempt, "status": cached["status"], "text": cached["parsed_text"],
                         "markdown": cached["parsed_markdown"], "structure": cached["structure_storage_key"],
                         "language": cached["language_code"], "flags": cached["quality_flags"]})
                await session.execute(text("""
                    INSERT INTO inha_policy.document_blocks
                      (id, document_id, parent_block_id, block_index, block_kind, text_content,
                       markdown_content, table_data, page_number, source_path, bbox, binary_asset_id,
                       ocr_used, ocr_confidence, extraction_metadata)
                    SELECT gen_random_uuid(), :new_document, NULL, block_index, block_kind,
                           text_content, markdown_content, table_data, page_number, source_path,
                           bbox, binary_asset_id, ocr_used, ocr_confidence,
                           extraction_metadata || jsonb_build_object('reused_from_document', CAST(:cached AS text))
                    FROM inha_policy.document_blocks WHERE document_id=:cached ORDER BY block_index
                """), {"new_document": document_id, "cached": cached["id"]})
                await session.execute(text(
                    "UPDATE inha_policy.documents SET sealed_at=now() WHERE id=:id"
                ), {"id": document_id})
                return
        result = await self._transcribe_images(
            session, self.parsers.parse(raw, filename, detected),
            config["llm.provider"]["value"], vision_model)
        if origin == "html_body":
            result = with_title_block(result, source["title"])
        attempt = (await session.execute(text("""
            SELECT coalesce(max(attempt_no), 0)+1 FROM inha_policy.documents
            WHERE notice_version_id=:version AND asset_occurrence_id IS NOT DISTINCT FROM :asset
              AND parser_name=:name AND parser_version=:parser_version
              AND parser_config_sha256=:config AND normalizer_version='blocks-1.0'
        """), {"version": job["notice_version_id"], "asset": occurrence_id,
                 "name": result.parser_name, "parser_version": result.parser_version,
                 "config": config_hash})).scalar_one()
        document_id = uuid.uuid4()
        now = datetime.now(UTC)
        await session.execute(text("""
            INSERT INTO inha_policy.documents
              (id, notice_version_id, asset_occurrence_id, origin_kind, parser_name,
               parser_version, parser_config_sha256, normalizer_version, attempt_no, status,
               parsed_text, quality_flags, started_at, finished_at)
            VALUES (:id, :version, :asset, :origin, :name, :parser_version, :config,
                    'blocks-1.0', :attempt, :status, :parsed_text, :flags, :now, :now)
        """), {"id": document_id, "version": job["notice_version_id"], "asset": occurrence_id,
                 "origin": origin, "name": result.parser_name, "parser_version": result.parser_version,
                 "config": config_hash, "attempt": attempt, "status": result.status,
                 "parsed_text": _db_text(result.text), "flags": list(result.quality_flags), "now": now})
        if origin == "asset":
            for derived in result.derived:
                stored = self.store.put(derived.data)
                await session.execute(text("""
                    INSERT INTO inha_policy.derived_files
                      (binary_asset_id, kind, tool, storage_key, sha256, byte_size)
                    VALUES (:asset, :kind, :tool, :key, :sha, :size)
                    ON CONFLICT (binary_asset_id, kind, tool) DO NOTHING
                """), {"asset": source["binary_asset_id"], "kind": derived.kind, "tool": derived.tool,
                         "key": stored.storage_key, "sha": stored.sha256, "size": stored.byte_size})
        for index, block in enumerate(result.blocks):
            block_id = uuid.uuid5(document_id, block.stable_key)
            await session.execute(text("""
                INSERT INTO inha_policy.document_blocks
                  (id, document_id, block_index, block_kind, text_content, table_data,
                   page_number, source_path, bbox, ocr_used, ocr_confidence, extraction_metadata)
                VALUES (:id, :document, :index, :kind, :content, CAST(:table_data AS jsonb),
                        :page, :path, :bbox, :ocr, :confidence, CAST(:metadata AS jsonb))
            """), {"id": block_id, "document": document_id, "index": index, "kind": block.kind,
                     "content": _db_text(block.text), "table_data": _db_json(block.table_data)
                     if block.table_data is not None else None,
                     "page": block.page_number, "path": block.source_path,
                     "bbox": list(block.bbox) if block.bbox else None, "ocr": block.ocr_used,
                     "confidence": block.ocr_confidence,
                     "metadata": _db_json({**block.metadata, "stable_key": block.stable_key})})
        await session.execute(text(
            "UPDATE inha_policy.documents SET sealed_at=now() WHERE id=:id"
        ), {"id": document_id})

    async def _transcribe_images(self, session: AsyncSession, result: ParseResult,
                                 provider_name: str, model: str) -> ParseResult:
        """Send every picture a parser found to the multimodal LLM; no local OCR fallback.

        A provider failure raises, so the job is retried and finally lands in review.
        """
        if not result.images:
            return result
        provider = ProviderRegistry(await resolved_settings(session)).llm(provider_name)
        semaphore = asyncio.Semaphore(TRANSCRIPTION_CONCURRENCY)

        async def transcribe(image: Any) -> tuple[str, str | None]:
            prepared = prepare_image(image.data)
            if prepared is None:
                return image.key, None
            if image.metadata.get("mode") == "page_supplement":
                prompt, version = page_prompt(image.metadata.get("page_text", "")), VISION_PAGE_PROMPT_VERSION
            else:
                prompt, version = VISION_PROMPT, VISION_PROMPT_VERSION
            cache_key = (hashlib.sha256(prepared).hexdigest(), model, version)
            if cache_key not in self.transcription_cache:
                async with semaphore:
                    response = await provider.transcribe_image(
                        model=model, image=prepared, mime="image/jpeg", prompt=prompt)
                self.transcription_cache[cache_key] = response.text
            return image.key, self.transcription_cache[cache_key]

        # Calls within one document run side by side; a failure still fails the job (retried).
        transcriptions = dict(await asyncio.gather(*(transcribe(image) for image in result.images)))
        return merge_transcriptions(result, transcriptions, model)

    async def _enqueue_structure_if_ready(self, session: AsyncSession, job: dict[str, Any]) -> None:
        pending = (await session.execute(text("""
            SELECT count(*) FROM inha_policy.crawl_jobs
            WHERE crawl_run_id=:run AND notice_version_id=:version AND stage='parse_document'
              AND id<>:job AND status IN ('queued', 'retry', 'running')
        """), {"run": job["crawl_run_id"], "version": job["notice_version_id"],
                 "job": job["id"]})).scalar_one()
        if pending == 0:
            key = f"structure:{job['notice_version_id']}"
            if str(job["job_key"]).startswith("reparse:"):
                waiting = (await session.execute(text("""
                    SELECT EXISTS (SELECT 1 FROM inha_policy.crawl_jobs
                                   WHERE notice_version_id=:version AND stage='structure'
                                     AND status IN ('queued', 'retry'))
                """), {"version": job["notice_version_id"]})).scalar_one()
                if waiting:
                    return  # the pending extraction will read the newest attempt anyway
                # The original structure job already ran; a reparse must re-extract.
                key = f"{key}:after:{job['id']}"
            await session.execute(text("""
                INSERT INTO inha_policy.crawl_jobs
                  (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
                VALUES (:run, 'structure', :key, :notice, :version, '{}'::jsonb)
                ON CONFLICT DO NOTHING
            """), {"run": job["crawl_run_id"], "key": key,
                     "notice": job["notice_id"], "version": job["notice_version_id"]})

    async def _structure(self, session: AsyncSession, job: dict[str, Any]) -> None:
        scope = (await session.execute(text("""
            SELECT n.scope_status FROM inha_policy.notices n
            JOIN inha_policy.notice_versions nv ON nv.notice_id=n.id WHERE nv.id=:version
        """), {"version": job["notice_version_id"]})).scalar_one()
        if scope != "included":
            # Out-of-scope notices keep their raw/parsed data but never reach the LLM.
            logger.info("skipping extraction for %s notice version %s", scope, job["notice_version_id"])
            return
        blocks, missing, manifest, input_hash = await extraction_input(session, job["notice_version_id"])
        if not blocks:
            raise ValueError("no parsed blocks are available for extraction")
        registry = ProviderRegistry(await resolved_settings(session))
        primary, crosscheck = registry.extraction(), registry.crosscheck()
        context = (job, blocks, missing, manifest, input_hash)
        # Re-assembling (split, forced match) works on the sealed extraction and never calls a model.
        reassembly = bool(job["payload"].get("forced_opportunity_id")
                          or job["payload"].get("target_item_index") is not None)
        # A temporarily unavailable provider (overload, rate limit, 5xx) is waited out: the job is
        # retried later, and only its last attempt falls back to the cross-check model.
        retry_later = not reassembly and job["attempt_count"] < job["max_attempts"]
        try:
            outcome = await self._extract(session, primary, *context, cache_only=reassembly)
        except Exception as exc:
            if retry_later and is_transient(exc):
                raise TransientProviderError(f"{primary.model} unavailable: {exc}"[:2000]) from exc
            if crosscheck is None:
                raise
            logger.warning("primary extraction %s (%s); falling back to %s",
                           "has no sealed run" if isinstance(exc, LookupError) else "failed",
                           exc.__class__.__name__, crosscheck.model)
            outcome = await self._extract(session, crosscheck, *context, cache_only=reassembly)
        else:
            if crosscheck is not None and needs_crosscheck(blocks, outcome.bundle):
                # Re-assembling a sealed result (split, retry) must reach the same decision without
                # calling a model, so the cross-check then comes from its sealed run or not at all.
                outcome = await self._crosscheck(session, outcome, crosscheck, context,
                                                 cache_only=outcome.reused, retry_later=retry_later)
        title = (await session.execute(text(
            "SELECT title FROM inha_policy.notice_versions WHERE id=:id"
        ), {"id": job["notice_version_id"]})).scalar_one()
        title_id = next((key for key, value in blocks.items() if value.strip() == title.strip()), None)
        if title_id is not None:  # older parses have no title block to quote
            outcome = ExtractionOutcome(outcome.run_id, with_title_revision(outcome.bundle, title_id, title),
                                        outcome.validation_errors, outcome.model, outcome.reused)
        assembler = OpportunityAssembler(session)
        forced = job["payload"].get("forced_opportunity_id")
        decision = job["payload"].get("identity_decision_id")
        version_ids = await assembler.assemble(bundle=outcome.bundle, extraction_run_id=outcome.run_id,
                                 notice_version_id=job["notice_version_id"],
                                 crawl_run_id=job["crawl_run_id"],
                                 evidence_warnings=outcome.validation_errors,
                                 forced_opportunity_id=uuid.UUID(forced) if forced else None,
                                 target_item_index=job["payload"].get("target_item_index"),
                                 identity_decision_id=uuid.UUID(decision) if decision else None)
        if not forced:
            await IdentityCandidateService(session).propose_for_versions(version_ids)

    async def _crosscheck(self, session: AsyncSession, first: "ExtractionOutcome",
                          target: ExtractionTarget, context: tuple, cache_only: bool = False,
                          retry_later: bool = False) -> "ExtractionOutcome":
        """Extract again with the second model; if the two results differ, merge them.

        Agreeing results keep the first. Differing ones (track count, application deadlines,
        amounts, headcounts) are merged by the second model against the source, and the merged
        result goes on as normal. If merging fails, the result with more opportunities is kept and
        the disagreement sends its versions to review.
        """
        try:
            second = await self._extract(session, target, *context, cache_only=cache_only)
        except LookupError:
            return first
        except Exception as exc:
            if retry_later and is_transient(exc):
                raise TransientProviderError(f"{target.model} unavailable: {exc}"[:2000]) from exc
            logger.warning("cross-check with %s failed: %s", target.model, exc.__class__.__name__)
            return first
        differences = extraction_differences(first.bundle, second.bundle)
        if not differences:
            return first
        job, blocks, missing, manifest, input_hash = context
        try:
            return await self._extract(
                session, target, job, blocks, missing, manifest, input_hash, cache_only=cache_only,
                system=MERGE_SYSTEM_PROMPT, prompt_version=MERGE_PROMPT_VERSION,
                candidates={"A": first.bundle.model_dump(mode="json"),
                            "B": second.bundle.model_dump(mode="json")},
                merged_from=[first.run_id, second.run_id], differences=differences)
        except Exception as exc:
            if retry_later and is_transient(exc):
                raise TransientProviderError(f"{target.model} unavailable: {exc}"[:2000]) from exc
            if not isinstance(exc, LookupError):
                logger.warning("merging the cross-checked results failed: %s", exc.__class__.__name__)
        counts = (len(first.bundle.opportunities), len(second.bundle.opportunities))
        chosen = second if counts[1] > counts[0] else first
        note = (f"crosscheck_disagreement ({', '.join(differences)}): {first.model} found {counts[0]} "
                f"item(s), {second.model} found {counts[1]}; kept {chosen.model}")
        bundle = chosen.bundle.model_copy(update={"warnings": [*chosen.bundle.warnings, note]})
        return ExtractionOutcome(chosen.run_id, bundle, chosen.validation_errors, chosen.model, chosen.reused)

    async def _extract(self, session: AsyncSession, target: ExtractionTarget, job: dict[str, Any],
                       blocks: dict[str, str], missing: list[uuid.UUID], manifest: dict[str, Any],
                       input_hash: str, cache_only: bool = False, *,
                       system: str = EXTRACTION_SYSTEM_PROMPT_V2,
                       prompt_version: str = EXTRACTION_PROMPT_VERSION_V2,
                       candidates: dict[str, Any] | None = None,
                       merged_from: list[uuid.UUID] | None = None,
                       differences: list[str] | None = None) -> "ExtractionOutcome":
        """One model's extraction run: reuse a sealed run of the same input, else call the model.

        The prompt names blocks "b1", "b2", ... in manifest order; stored outputs use block ids.
        A merge run also gets the two candidate results and is keyed by the runs it merges.
        """
        forward, backward = short_block_ids(blocks)
        short_blocks = {forward[key]: value for key, value in blocks.items()}
        prompt_hash = hashlib.sha256(system.encode()).hexdigest()
        if merged_from:
            input_hash = hashlib.sha256(
                (input_hash + "".join(str(run) for run in merged_from)).encode()).hexdigest()
        keys = {"version": job["notice_version_id"], "schema": EXTRACTION_SCHEMA_VERSION_V2,
                "prompt": prompt_version, "prompt_sha": prompt_hash,
                "provider": target.provider_name, "model": target.model, "input_sha": input_hash}
        cached = (await session.execute(text("""
            SELECT id, status, parsed_output, validation_errors
            FROM inha_policy.extraction_runs
            WHERE notice_version_id=:version AND schema_version=:schema
              AND prompt_version=:prompt AND prompt_sha256=:prompt_sha
              AND provider_name=:provider AND model_id=:model
              AND input_snapshot_sha256=:input_sha
              AND status='succeeded' AND parsed_output IS NOT NULL AND sealed_at IS NOT NULL
            ORDER BY finished_at DESC LIMIT 1
        """), keys)).mappings().one_or_none()
        if cached:
            return ExtractionOutcome(cached["id"], ExtractionBundleV2.model_validate(cached["parsed_output"]),
                                     list(cached["validation_errors"] or []), target.model, reused=True)
        if cache_only:
            raise LookupError(f"no sealed {target.model} extraction for this input")
        extraction_id = uuid.uuid4()
        self.current_extraction_run_id = extraction_id
        log = EXCHANGE_LOG.get()
        first_exchange = len(log) if log is not None else 0
        await session.execute(text("""
            INSERT INTO inha_policy.extraction_runs
              (id, notice_version_id, schema_version, prompt_version, prompt_sha256,
               provider_name, model_id, processing_code_version, model_parameters,
               input_manifest, input_snapshot_sha256, status, started_at)
            VALUES (:id, :version, :schema, :prompt, :prompt_sha, :provider, :model,
                    'policy-harvester-0.2.0', CAST(:parameters AS jsonb),
                    CAST(:manifest AS jsonb), :input_sha, 'running', now())
        """), {**keys, "id": extraction_id, "parameters": json.dumps(target.parameters),
                 "manifest": json.dumps({**manifest, "block_aliases": "b<n> = n-th block in manifest order",
                                         **({"merged_from": [str(run) for run in merged_from],
                                             "differences": differences} if merged_from else {})})})
        await session.commit()
        try:
            result = await self._revalidate_failed_output(session, keys, backward)
            if result is None:
                payload: dict[str, Any] = {
                    "blocks": [{"block_id": key, "text": value} for key, value in short_blocks.items()],
                    "missing_document_ids": [str(item) for item in missing]}
                if candidates:
                    payload["candidates"] = restore_block_ids(candidates, forward)  # block ids -> b<n>
                result = await target.provider.structured(
                    model=target.model, system=system, payload=payload,
                    schema=ExtractionBundleV2, parameters=dict(target.parameters))
        except Exception as exc:
            # Keep whatever the provider returned, even when it never validated.
            raw = getattr(exc, "raw_response", None)
            await session.execute(text("""
                UPDATE inha_policy.extraction_runs SET status='failed', error_code=:code,
                  error_message=:message, raw_response=CAST(:raw AS jsonb),
                  finished_at=now(), sealed_at=now() WHERE id=:id
            """), {"code": exc.__class__.__name__, "message": str(exc)[:4000],
                     "raw": json.dumps(raw) if raw is not None else None, "id": extraction_id})
            await session.commit()
            raise
        finally:
            for entry in (log or [])[first_exchange:]:
                entry["extraction_run_id"] = extraction_id
        short_bundle = ExtractionBundleV2.model_validate(result.value)
        validation_errors = validate_evidence(short_bundle, short_blocks)
        bundle = ExtractionBundleV2.model_validate(
            restore_block_ids(short_bundle.model_dump(mode="json"), backward))
        if missing and bundle.coverage == "complete":
            bundle = bundle.model_copy(update={
                "coverage": "partial",
                "warnings": [*bundle.warnings,
                             "application downgraded coverage because one or more documents were not parsed"],
            })
        if validation_errors:
            bundle = bundle.model_copy(update={"warnings": [
                *bundle.warnings, *[f"evidence_validation_warning: {error}" for error in validation_errors]]})
        await session.execute(text("""
            UPDATE inha_policy.extraction_runs SET status='succeeded',
              raw_response=CAST(:raw AS jsonb), parsed_output=CAST(:parsed AS jsonb),
              validation_errors=CAST(:errors AS jsonb), quality_flags=:quality_flags,
              input_tokens=:input_tokens, output_tokens=:output_tokens,
              finished_at=now(), sealed_at=now() WHERE id=:id
        """), {"raw": json.dumps(result.raw_response), "parsed": bundle.model_dump_json(),
                 "errors": json.dumps(validation_errors),
                 "quality_flags": ["evidence_validation_warning"] if validation_errors else [],
                 "input_tokens": result.usage.input_tokens,
                 "output_tokens": result.usage.output_tokens, "id": extraction_id})
        await session.commit()
        return ExtractionOutcome(extraction_id, bundle, validation_errors, target.model)

    async def _revalidate_failed_output(self, session: AsyncSession, keys: dict[str, Any],
                                        backward: dict[str, str]) -> GenerationResult | None:
        """Reuse a preserved raw output of the same input if current validation now accepts it."""
        rows = (await session.execute(text("""
            SELECT id, raw_response FROM inha_policy.extraction_runs
            WHERE notice_version_id=:version AND schema_version=:schema AND prompt_version=:prompt
              AND prompt_sha256=:prompt_sha AND provider_name=:provider AND model_id=:model
              AND input_snapshot_sha256=:input_sha AND status='failed'
              AND raw_response ? 'responses'
            ORDER BY finished_at DESC LIMIT 3
        """), keys)).mappings().all()
        for row in rows:
            for response in reversed(row["raw_response"]["responses"]):
                try:
                    content = response["choices"][0]["message"]["content"]
                    value = validate_lenient_json(ExtractionBundleV2, content)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
                logger.info("revalidated preserved output of extraction run %s", row["id"])
                return GenerationResult(value, {"revalidated_from": str(row["id"]),
                                                "original_response": response}, Usage(),
                                        keys["provider"], keys["model"])
        return None

    async def _embed(self, session: AsyncSession, job: dict[str, Any]) -> None:
        version_id = uuid.UUID(job["payload"]["opportunity_version_id"])
        version = (await session.execute(text("""
            SELECT title, summary, provider_name, eligibility_summary, support_summary
            FROM inha_policy.opportunity_versions WHERE id=:id
        """), {"id": version_id})).mappings().one()
        config = await effective_configuration(session)
        profile = await ensure_profile(session, config)
        profile_id, provider_name, model, dimensions = (profile.id, profile.provider, profile.model,
                                                        profile.dimensions)
        parts = [version["title"], version["provider_name"], version["summary"],
                 version["support_summary"], version["eligibility_summary"]]
        chunk_text = "\n".join(value for value in parts if value)
        providers = ProviderRegistry(await resolved_settings(session))
        vectors = await providers.embedding(provider_name).embed(
            model=model, texts=[chunk_text], dimensions=dimensions)
        vector = vectors[0]
        if len(vector) != dimensions:
            raise ValueError(f"provider returned {len(vector)} dimensions; profile requires {dimensions}")
        magnitude = math.sqrt(sum(value * value for value in vector))
        if magnitude == 0:
            raise ValueError("embedding provider returned a zero vector")
        vector = [value / magnitude for value in vector]
        vector_literal = "[" + ",".join(str(value) for value in vector) + "]"
        input_sha = hashlib.sha256(chunk_text.encode()).hexdigest()
        await session.execute(text("""
            INSERT INTO inha_policy.search_chunks
              (opportunity_version_id, embedding_profile_id, chunk_key, chunk_kind,
               ordinal, chunk_text, input_sha256, lexical_tokens, embedding,
               embedding_status, embedding_created_at)
            VALUES (:version, :profile, 'summary', 'summary', 0, :text, :sha, :lexical,
                    CAST(:embedding AS vector), 'succeeded', now())
            ON CONFLICT (opportunity_version_id, embedding_profile_id, chunk_key) DO UPDATE
              SET chunk_text=EXCLUDED.chunk_text, input_sha256=EXCLUDED.input_sha256,
                  lexical_tokens=EXCLUDED.lexical_tokens, embedding=EXCLUDED.embedding,
                  embedding_status='succeeded', embedding_created_at=now(), embedding_error=NULL
              WHERE inha_policy.search_chunks.embedding_status='failed'
        """), {"version": version_id, "profile": profile_id, "text": chunk_text,
                 "sha": input_sha, "lexical": chunk_text, "embedding": vector_literal})
        if await activate_if_complete(session, profile):
            logger.info("search switched to embedding profile %s", profile.key)
        # Embedding backfills (recompute) carry publish=false: they index, they never publish.
        if bool(config["publishing.auto_publish"]["value"]) and job["payload"].get("publish", True):
            # Database publication guards have the final say; a refusal keeps the draft and
            # the embedding instead of failing the whole job.
            try:
                async with session.begin_nested():
                    candidate = (await session.execute(text("""
                        UPDATE inha_policy.opportunity_versions ov
                        SET publication_state='published', published_at=now()
                        WHERE ov.id=:id AND ov.publication_state='draft'
                          AND ov.data_quality_status='complete'
                          AND NOT EXISTS (
                            SELECT 1 FROM inha_policy.opportunity_version_sources s
                            JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                            JOIN inha_policy.notices n ON n.id=nv.notice_id
                            WHERE s.opportunity_version_id=ov.id AND s.is_current_dependency
                              AND (n.scope_status <> 'included'
                                   OR n.availability_status <> 'available'
                                   OR n.current_notice_version_id IS DISTINCT FROM nv.id))
                        RETURNING ov.opportunity_id
                    """), {"id": version_id})).scalar_one_or_none()
            except DBAPIError as exc:
                candidate = None
                await session.execute(text("""
                    INSERT INTO inha_policy.review_items
                      (review_kind, entity_type, entity_id, payload)
                    VALUES ('other', 'opportunity_version', :id, CAST(:payload AS jsonb))
                """), {"id": version_id, "payload": json.dumps({
                    "operation": "auto_publish_blocked",
                    "error": str(exc.orig or exc).splitlines()[0][:500]}, ensure_ascii=False)})
            if candidate:
                await session.execute(text("""
                    UPDATE inha_policy.opportunities SET current_version_id=:version,
                      lifecycle_status='active', merged_into_id=NULL, updated_at=now()
                    WHERE id=:opportunity
                """), {"version": version_id, "opportunity": candidate})
                await session.execute(text("""
                    INSERT INTO inha_policy.audit_logs
                      (actor_kind, actor_id, action, entity_type, entity_id, after_state,
                       reason, automated)
                    VALUES ('worker', 'publisher', 'auto_published', 'opportunity',
                            CAST(:opportunity AS text),
                            jsonb_build_object('opportunity_version_id', CAST(:version AS text)),
                            'high-confidence automatic publication', true)
                """), {"opportunity": candidate, "version": version_id})


async def run_worker(forever: bool, drain: bool = False, max_jobs: int | None = None,
                     stages: tuple[str, ...] = STAGES) -> None:
    worker = Worker(stages)
    # docker stop sends SIGTERM to PID 1: cancel the running job so it is returned to the queue.
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, task.cancel)
    try:
        await _worker_loop(worker, forever, drain, max_jobs)
    except asyncio.CancelledError:
        logger.info("worker stopped by signal")


async def _worker_loop(worker: Worker, forever: bool, drain: bool, max_jobs: int | None) -> None:
    processed = 0
    while True:
        worked = await worker.run_once()
        processed += int(worked)
        if max_jobs is not None and processed >= max_jobs:
            break
        if drain and not worked:
            break
        if not forever and not drain:
            break
        if not worked:
            await asyncio.sleep(2)
    logger.info("worker stopped after %s jobs", processed)


async def abandon_interrupted_runs(session: AsyncSession, *, older_than: timedelta | None = None) -> int:
    """Mark crawl runs left "running" by a dead process as failed, so the source can crawl again.

    Crawls run inside the scheduler process, so when the scheduler starts every "running" run
    belongs to a process that is gone (a restart, a redeploy). Without this, the one-active-run
    rule blocks the source forever. While running, runs older than ``older_than`` are swept too.
    """
    condition = "started_at < now() - CAST(:age AS interval)" if older_than else "true"
    rows = (await session.execute(text(f"""
        UPDATE inha_policy.crawl_runs SET status='failed', finished_at=now(),
          error_summary=coalesce(error_summary || ' / ', '') || :reason
        WHERE status='running' AND {condition} RETURNING id
    """), {"reason": "interrupted: the crawling process stopped before the run finished",
             "age": f"{int(older_than.total_seconds())} seconds" if older_than else None})).all()
    if rows:
        logger.warning("marked %d interrupted crawl run(s) as failed", len(rows))
    await session.commit()
    return len(rows)


STALE_CRAWL_RUN = timedelta(hours=6)


async def run_scheduler(forever: bool) -> None:
    async with SessionFactory() as session:
        await abandon_interrupted_runs(session)
    while True:
        async with SessionFactory() as session:
            await abandon_interrupted_runs(session, older_than=STALE_CRAWL_RUN)
            queued = (await session.execute(text("""
                SELECT r.id, r.mode, s.source_key FROM inha_policy.crawl_runs r
                JOIN inha_policy.sources s ON s.id=r.source_id
                WHERE r.status='queued' ORDER BY r.scheduled_for LIMIT 1
            """))).mappings().one_or_none()
            if queued:
                crawler = CrawlService(session)
                try:
                    await crawler.run(queued["source_key"], queued["mode"], queued["id"])
                finally:
                    await crawler.close()
            sources = (await session.execute(text("""
                SELECT source_key FROM inha_policy.sources s WHERE enabled
                  AND (last_successful_run_at IS NULL OR last_successful_run_at < now() - interval '20 hours')
                  AND NOT EXISTS (SELECT 1 FROM inha_policy.crawl_runs r
                                  WHERE r.source_id=s.id AND r.status IN ('queued', 'running'))
            """))).scalars().all()
            for source_key in sources:
                crawler = CrawlService(session)
                try:
                    await crawler.run(source_key)
                finally:
                    await crawler.close()
        if not forever:
            return
        await asyncio.sleep(60)


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "schedule"):
        child = subparsers.add_parser(command)
        child.add_argument("--forever", action="store_true")
        if command == "run":
            # Batch mode: stop when the queue is empty or after a job budget.
            child.add_argument("--drain", action="store_true")
            child.add_argument("--max-jobs", type=int)
            # e.g. --stages parse_document: parse everything without spending LLM calls.
            child.add_argument("--stages", default=",".join(STAGES))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.command == "run":
        stages = tuple(item.strip() for item in args.stages.split(",") if item.strip())
        if not stages or set(stages) - set(STAGES):
            parser.error(f"--stages must be a subset of {','.join(STAGES)}")
    asyncio.run(run_worker(args.forever, args.drain, args.max_jobs, stages) if args.command == "run"
                else run_scheduler(args.forever))


if __name__ == "__main__":
    main()
