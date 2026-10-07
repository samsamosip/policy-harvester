from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings, source_proxy_url
from ..documents.types import detect_type
from ..storage import ObjectStore, build_object_store
from .adapters import SourceAdapter, adapter_for

logger = logging.getLogger(__name__)
HEADER_ALLOWLIST = {"content-type", "content-length", "etag", "last-modified", "date", "location", "retry-after"}


@dataclass
class FetchResult:
    snapshot_id: uuid.UUID
    outcome: str
    status_code: int | None
    payload: bytes | None
    asset_id: uuid.UUID | None
    headers: dict[str, str]
    final_url: str | None
    error: str | None = None


class CrawlService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None,
                 store: ObjectStore | None = None):
        self.session = session
        self.settings = settings or get_settings()
        self.store = store or build_object_store(self.settings)
        self.client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def run(self, source_key: str, mode: str = "daily_full",
                  run_id: uuid.UUID | None = None, *, max_pages: int | None = None,
                  max_notices: int | None = None) -> uuid.UUID:
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        if max_notices is not None and max_notices < 1:
            raise ValueError("max_notices must be at least 1")
        source = (await self.session.execute(text(
            "SELECT * FROM inha_policy.sources WHERE source_key=:key AND enabled"
        ), {"key": source_key})).mappings().one()
        source = dict(source)
        proxy_url = await source_proxy_url(self.session, source_key)
        transport_proxy = proxy_url.replace("socks5h://", "socks5://", 1) if proxy_url else None
        self.client = httpx.AsyncClient(
            timeout=self.settings.crawl_timeout_seconds,
            follow_redirects=True,
            proxy=transport_proxy,
            verify=self.settings.crawl_tls_verify,
            trust_env=False,
            headers={"User-Agent": self.settings.crawl_user_agent,
                     "Accept-Language": "ko-KR,ko;q=0.9"},
        )
        if run_id is None:
            run_id = uuid.uuid4()
            await self.session.execute(text("""
                INSERT INTO inha_policy.crawl_runs
                  (id, source_id, mode, status, scheduled_for, started_at)
                VALUES (:id, :source_id, :mode, 'running', now(), now())
            """), {"id": run_id, "source_id": source["id"], "mode": mode})
        else:
            await self.session.execute(text("""
                UPDATE inha_policy.crawl_runs SET status='running', started_at=now()
                WHERE id=:id AND source_id=:source_id AND status='queued'
            """), {"id": run_id, "source_id": source["id"]})
        await self.session.commit()
        adapter = adapter_for(source)
        stats = {"discovered": 0, "fetched": 0, "changed": 0, "failed": 0,
                 "attachments_changed": 0, "parser_errors": 0, "download_errors": 0,
                 "missing_candidates": 0, "proxy_enabled": bool(proxy_url),
                 "proxy_scheme": urlsplit(proxy_url).scheme if proxy_url else None,
                 "tls_verify": self.settings.crawl_tls_verify,
                 "listing_complete": max_pages is None and max_notices is None,
                 "max_pages": max_pages, "max_notices": max_notices}
        status = "succeeded"
        error_summary = None
        cancelled = False
        try:
            discovered = await self._discover(source, run_id, adapter, stats,
                                              max_pages=max_pages)
            stats["discovered"] = len(discovered)
            selected = list(discovered.items())
            if max_notices is not None:
                selected = selected[:max_notices]
            for external_id, item in selected:
                try:
                    changed = await self._collect_notice(source, run_id, adapter, item, stats)
                    stats["fetched"] += 1
                    stats["changed"] += int(changed)
                except Exception as exc:
                    logger.exception("notice collection failed", extra={"external_id": external_id})
                    stats["failed"] += 1
                    error_summary = str(exc)
                    await self.session.rollback()
            if stats["listing_complete"]:
                await self._mark_missing(source, run_id, set(discovered), stats)
            if stats["failed"]:
                status = "partial" if stats["fetched"] else "failed"
        except asyncio.CancelledError:
            await self.session.rollback()
            status, error_summary, cancelled = "cancelled", "crawl cancelled", True
        except Exception as exc:
            logger.exception("crawl failed", extra={"source_key": source_key})
            await self.session.rollback()
            status, error_summary = "failed", str(exc)
        await self.session.execute(text("""
            UPDATE inha_policy.crawl_runs SET status=:status, finished_at=now(),
              discovered_count=:discovered, fetched_count=:fetched, changed_count=:changed,
              failed_count=:failed, metrics=CAST(:metrics AS jsonb), error_summary=:error
            WHERE id=:id
        """), {"status": status, "discovered": stats["discovered"], "fetched": stats["fetched"],
                 "changed": stats["changed"], "failed": stats["failed"],
                 "metrics": json.dumps(stats), "error": error_summary, "id": run_id})
        if status in {"succeeded", "partial"} and stats["listing_complete"]:
            await self.session.execute(text(
                "UPDATE inha_policy.sources SET last_successful_run_at=now() WHERE id=:id"
            ), {"id": source["id"]})
        await self.session.commit()
        if cancelled:
            raise asyncio.CancelledError
        return run_id

    async def _discover(self, source: dict[str, Any], run_id: uuid.UUID,
                        adapter: SourceAdapter, stats: dict[str, Any], *,
                        max_pages: int | None = None) -> dict[str, dict[str, Any]]:
        discovered: dict[str, dict[str, Any]] = {}
        total_pages = 1
        page = 1
        while page <= total_pages:
            url = adapter.list_page_url(source, page)
            fetched = await self._fetch(source, run_id, None, uuid.uuid4(), "list",
                                        f"list:{page}", url)
            if fetched.payload is None:
                raise RuntimeError(f"list page {page} unavailable: {fetched.error or fetched.status_code}")
            try:
                parsed = adapter.parse_list(fetched.payload, url)
            except Exception as exc:
                stats["parser_errors"] += 1
                stats["failed"] += 1
                await self._queue_review("parsing_failed", "source", source["id"],
                                         {"stage": "list", "page": page, "error": str(exc)})
                await self.session.commit()
                raise RuntimeError(f"list parser failed on page {page}: {exc}") from exc
            total_pages = parsed["total_pages"]
            for item in parsed["items"]:
                discovered[item["external_article_id"]] = item
            page += 1
            await self.session.commit()
            if max_pages is not None and page > max_pages:
                break
        return discovered

    async def _collect_notice(self, source: dict[str, Any], run_id: uuid.UUID,
                              adapter: SourceAdapter, listed: dict[str, Any],
                              stats: dict[str, Any], *, listed_presence: bool = True) -> bool:
        now = datetime.now(UTC)
        notice_id = (await self.session.execute(text("""
            INSERT INTO inha_policy.notices
              (source_id, external_post_id, canonical_url, first_seen_at, last_seen_at)
            VALUES (:source_id, :external_id, :url, :now, :now)
            ON CONFLICT (source_id, external_post_id) DO UPDATE
              SET canonical_url=EXCLUDED.canonical_url,
                  last_seen_at=CASE WHEN :listed_presence THEN EXCLUDED.last_seen_at
                                    ELSE inha_policy.notices.last_seen_at END
            RETURNING id
        """), {"source_id": source["id"], "external_id": listed["external_article_id"],
                 "url": listed["canonical_url"], "now": now,
                 "listed_presence": listed_presence})).scalar_one()
        collection_id = uuid.uuid4()
        detail_fetch = await self._fetch(source, run_id, notice_id, collection_id, "detail",
                                         f"detail:{listed['external_article_id']}", listed["canonical_url"])
        if detail_fetch.payload is None:
            await self._record_unavailable(notice_id, detail_fetch.status_code)
            await self.session.commit()
            return False
        try:
            detail = adapter.parse_detail(detail_fetch.payload, listed["canonical_url"])
        except Exception as exc:
            stats["parser_errors"] += 1
            stats["failed"] += 1
            await self._queue_review("parsing_failed", "notice", notice_id,
                                     {"stage": "detail", "error": str(exc)})
            await self.session.commit()
            return False
        # A later successful parse settles earlier detail-parse failures of the same notice.
        await self.session.execute(text("""
            UPDATE inha_policy.review_items
            SET status='resolved', resolved_at=now(), updated_at=now(),
                resolution_note='resolved automatically: a later crawl parsed this notice'
            WHERE entity_type='notice' AND entity_id=:notice AND review_kind='parsing_failed'
              AND status IN ('open', 'in_review') AND payload->>'stage'='detail'
        """), {"notice": notice_id})

        occurrences: list[dict[str, Any]] = []
        for asset in adapter.assets(detail):
            fetched = await self._fetch(source, run_id, notice_id, collection_id, asset["role"],
                                        asset["occurrence_key"], asset["url"],
                                        parent_snapshot_id=detail_fetch.snapshot_id,
                                        filename=asset.get("filename"))
            occurrences.append({**asset, "fetch": fetched})
            if fetched.payload is None:
                stats["download_errors"] += 1

        fingerprint_manifest = {
            "html_semantic_sha256": detail["html_semantic_sha256"],
            "assets": [{"occurrence_key": item["occurrence_key"],
                        "sha256": hashlib.sha256(item["fetch"].payload).hexdigest()
                        if item["fetch"].payload is not None else None,
                        "status": item["fetch"].outcome} for item in occurrences],
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        current = (await self.session.execute(text("""
            SELECT nv.id, nv.content_fingerprint
            FROM inha_policy.notices n LEFT JOIN inha_policy.notice_versions nv
              ON nv.id=n.current_notice_version_id WHERE n.id=:id
        """), {"id": notice_id})).mappings().one()
        previous_hashes: dict[str, str] = {}
        if current["id"] is not None:
            previous_rows = (await self.session.execute(text("""
                SELECT nva.occurrence_key, ba.sha256
                FROM inha_policy.notice_version_assets nva
                JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
                WHERE nva.notice_version_id=:version
            """), {"version": current["id"]})).mappings().all()
            previous_hashes = {row["occurrence_key"]: row["sha256"] for row in previous_rows}
            for item in fingerprint_manifest["assets"]:
                if item["sha256"] is None and item["occurrence_key"] in previous_hashes:
                    item["sha256"] = previous_hashes[item["occurrence_key"]]
                    item["status"] = "received"
            fingerprint = hashlib.sha256(json.dumps(
                fingerprint_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            current_hashes = {item["occurrence_key"]: item["sha256"]
                              for item in fingerprint_manifest["assets"]}
            if current_hashes != previous_hashes:
                stats["attachments_changed"] += 1
        included = adapter.includes(source, detail)
        if listed_presence:
            await self.session.execute(text("""
                UPDATE inha_policy.notices SET last_seen_at=now(), last_checked_at=now(),
                  last_content_verified_at=now(), last_http_status=200,
                  availability_status='available', consecutive_missing_count=0,
                  unavailable_since=NULL, scope_status=:scope WHERE id=:id
            """), {"scope": "included" if included else "excluded", "id": notice_id})
        else:
            await self.session.execute(text("""
                UPDATE inha_policy.notices SET last_checked_at=now(),
                  last_content_verified_at=now(), last_http_status=200,
                  consecutive_missing_count=consecutive_missing_count+1,
                  availability_status=CASE WHEN consecutive_missing_count+1 >= :threshold
                                           THEN 'unavailable' ELSE 'uncertain' END,
                  unavailable_since=coalesce(unavailable_since, now()), scope_status=:scope
                WHERE id=:id
            """), {"threshold": self.settings.source_missing_threshold,
                     "scope": "included" if included else "excluded", "id": notice_id})
        if current["content_fingerprint"] == fingerprint:
            await self.session.commit()
            return False

        revision_no = (await self.session.execute(text(
            "SELECT coalesce(max(revision_no), 0)+1 FROM inha_policy.notice_versions WHERE notice_id=:id"
        ), {"id": notice_id})).scalar_one()
        version_id = uuid.uuid4()
        html_object = self.store.put(detail_fetch.payload)
        asset_status = "complete" if all(item["fetch"].payload is not None for item in occurrences) else "partial"
        await self.session.execute(text("""
            INSERT INTO inha_policy.notice_versions
              (id, notice_id, revision_no, crawl_run_id, origin_fetch_snapshot_id,
               content_fingerprint, raw_html_sha256, raw_html_storage_key, title, author_name,
               category_name, is_pinned, published_on, source_modified_at, body_html, body_text,
               source_metadata, asset_collection_status, fetched_at, collection_started_at,
               collection_finished_at, sealed_at)
            VALUES
              (:id, :notice_id, :revision_no, :run_id, :snapshot_id, :fingerprint,
               :raw_sha, :raw_key, :title, :author, :category, :pinned, :published_on,
               :modified_at, :body_html, :body_text, CAST(:metadata AS jsonb), :asset_status,
               now(), now(), now(), NULL)
        """), {
            "id": version_id, "notice_id": notice_id, "revision_no": revision_no,
            "run_id": run_id, "snapshot_id": detail_fetch.snapshot_id, "fingerprint": fingerprint,
            "raw_sha": html_object.sha256, "raw_key": html_object.storage_key,
            "title": detail["title"], "author": detail.get("author"),
            "category": detail.get("source_category"), "pinned": listed.get("is_pinned", False),
            "published_on": detail.get("published_date"),
            "modified_at": None, "body_html": detail["content_html"],
            "body_text": detail["content_text"], "metadata": json.dumps(detail, ensure_ascii=False),
            "asset_status": asset_status,
        })
        for item in occurrences:
            fetched = item["fetch"]
            await self.session.execute(text("""
                INSERT INTO inha_policy.notice_version_assets
                  (notice_version_id, occurrence_key, ordinal, role, original_url, resolved_url,
                   original_filename, alt_text, reported_mime, binary_asset_id, download_status,
                   http_status, error_message, attempted_at, fetch_snapshot_id)
                VALUES (:version_id, :key, :ordinal, :role, :url, :resolved_url, :filename,
                        :alt, :mime, :asset_id, :status, :http_status, :error, now(), :snapshot_id)
            """), {"version_id": version_id, "key": item["occurrence_key"],
                     "ordinal": item["ordinal"], "role": item["role"], "url": item["url"],
                     "resolved_url": fetched.final_url, "filename": item.get("filename"),
                     "alt": item.get("alt"), "mime": fetched.headers.get("content-type"),
                     "asset_id": fetched.asset_id,
                     "status": "succeeded" if fetched.payload is not None else "failed",
                     "http_status": fetched.status_code, "error": fetched.error,
                     "snapshot_id": fetched.snapshot_id})
        await self.session.execute(text(
            "UPDATE inha_policy.notice_versions SET sealed_at=now() WHERE id=:id"
        ), {"id": version_id})
        await self.session.execute(text(
            "UPDATE inha_policy.notices SET current_notice_version_id=:version WHERE id=:notice"
        ), {"version": version_id, "notice": notice_id})
        await self.session.execute(text("""
            INSERT INTO inha_policy.audit_logs
              (actor_kind, actor_id, action, entity_type, entity_id, before_state,
               after_state, related_source_id, automated)
            VALUES ('worker', 'crawler', 'notice_version_created', 'notice', CAST(:notice AS text),
                    jsonb_build_object('notice_version_id', CAST(:before AS text)),
                    jsonb_build_object('notice_version_id', CAST(:after AS text),
                                       'content_fingerprint', CAST(:fingerprint AS text)), :source, true)
        """), {"notice": notice_id, "before": current["id"], "after": version_id,
                 "fingerprint": fingerprint, "source": source["id"]})
        if included:
            await self._enqueue_parse_jobs(run_id, notice_id, version_id)
        await self.session.commit()
        return True

    async def _fetch(self, source: dict[str, Any], run_id: uuid.UUID, notice_id: uuid.UUID | None,
                     collection_id: uuid.UUID, resource_kind: str, resource_key: str, url: str,
                     parent_snapshot_id: uuid.UUID | None = None,
                     filename: str | None = None) -> FetchResult:
        started = datetime.now(UTC)
        snapshot_id = uuid.uuid4()
        if self.client is None:
            raise RuntimeError("crawler HTTP client is not configured")
        try:
            response = await self.client.get(url)
            finished = datetime.now(UTC)
            headers = {key.lower(): value for key, value in response.headers.items()
                       if key.lower() in HEADER_ALLOWLIST}
            if 200 <= response.status_code < 300:
                payload = response.content
                detected = detect_type(payload, filename)
                stored = self.store.put(payload)
                asset_id = await self._ensure_binary(stored.sha256, stored.storage_key,
                                                     stored.byte_size, detected.mime,
                                                     {"detected_format": detected.format,
                                                      "confidence": detected.confidence})
                outcome, error = "received", None
            else:
                payload, asset_id, outcome = None, None, "http_error"
                error = f"HTTP {response.status_code}"
            await self.session.execute(text("""
                INSERT INTO inha_policy.fetch_snapshots
                  (id, source_id, crawl_run_id, collection_id, notice_id, parent_fetch_snapshot_id,
                   resource_kind, resource_key, requested_url, final_url, response_metadata,
                   redirect_chain, outcome, http_status, response_body_asset_id,
                   representation_asset_id, error_message, fetch_started_at, fetched_at)
                VALUES (:id, :source_id, :run_id, :collection_id, :notice_id, :parent_id,
                        :kind, :key, :url, :final_url, CAST(:headers AS jsonb), CAST(:redirects AS jsonb),
                        :outcome, :status, :asset_id, :asset_id, :error, :started, :finished)
            """), {"id": snapshot_id, "source_id": source["id"], "run_id": run_id,
                     "collection_id": collection_id, "notice_id": notice_id,
                     "parent_id": parent_snapshot_id, "kind": self._resource_kind(resource_kind),
                     "key": resource_key, "url": url, "final_url": str(response.url),
                     "headers": json.dumps(headers),
                     "redirects": json.dumps([{"status": prior.status_code, "url": str(prior.url)}
                                               for prior in response.history]),
                     "outcome": outcome, "status": response.status_code, "asset_id": asset_id,
                     "error": error, "started": started, "finished": finished})
            return FetchResult(snapshot_id, outcome, response.status_code, payload, asset_id,
                               headers, str(response.url), error)
        except httpx.HTTPError as exc:
            finished = datetime.now(UTC)
            await self.session.execute(text("""
                INSERT INTO inha_policy.fetch_snapshots
                  (id, source_id, crawl_run_id, collection_id, notice_id, parent_fetch_snapshot_id,
                   resource_kind, resource_key, requested_url, outcome, error_code, error_message,
                   fetch_started_at, fetched_at)
                VALUES (:id, :source_id, :run_id, :collection_id, :notice_id, :parent_id,
                        :kind, :key, :url, 'network_error', :code, :error, :started, :finished)
            """), {"id": snapshot_id, "source_id": source["id"], "run_id": run_id,
                     "collection_id": collection_id, "notice_id": notice_id,
                     "parent_id": parent_snapshot_id, "kind": self._resource_kind(resource_kind),
                     "key": resource_key, "url": url, "code": exc.__class__.__name__,
                     "error": str(exc), "started": started, "finished": finished})
            return FetchResult(snapshot_id, "network_error", None, None, None, {}, None, str(exc))

    @staticmethod
    def _resource_kind(role: str) -> str:
        return {"attachment": "attachment", "inline_image": "inline_image",
                "list": "list", "detail": "detail"}.get(role, "external_resource")

    async def _ensure_binary(self, sha256: str, storage_key: str, byte_size: int,
                             mime: str, metadata: dict[str, Any]) -> uuid.UUID:
        return (await self.session.execute(text("""
            INSERT INTO inha_policy.binary_assets
              (sha256, storage_key, detected_mime, byte_size, technical_metadata)
            VALUES (:sha, :key, :mime, :size, CAST(:metadata AS jsonb))
            ON CONFLICT (sha256) DO UPDATE SET sha256=EXCLUDED.sha256
            RETURNING id
        """), {"sha": sha256, "key": storage_key, "mime": mime, "size": byte_size,
                 "metadata": json.dumps(metadata)})).scalar_one()

    async def _record_unavailable(self, notice_id: uuid.UUID, status: int | None) -> None:
        threshold = self.settings.source_missing_threshold
        await self.session.execute(text("""
            UPDATE inha_policy.notices SET last_checked_at=now(), last_http_status=:status,
              consecutive_missing_count=consecutive_missing_count+1,
              availability_status=CASE WHEN consecutive_missing_count+1 >= :threshold
                                       THEN 'unavailable' ELSE 'uncertain' END,
              unavailable_since=coalesce(unavailable_since, now()) WHERE id=:id
        """), {"status": status, "threshold": threshold, "id": notice_id})

    async def _mark_missing(self, source: dict[str, Any], run_id: uuid.UUID,
                            seen_external_ids: set[str], stats: dict[str, Any]) -> None:
        rows = (await self.session.execute(text("""
            SELECT id, external_post_id, canonical_url FROM inha_policy.notices
            WHERE source_id=:source_id AND NOT (external_post_id = ANY(:seen))
        """), {"source_id": source["id"], "seen": list(seen_external_ids) or [""]})).mappings().all()
        stats["missing_candidates"] = len(rows)
        for row in rows:
            listed = {"external_article_id": row["external_post_id"],
                      "canonical_url": row["canonical_url"], "is_pinned": False}
            try:
                changed = await self._collect_notice(source, run_id, adapter_for(source), listed,
                                                     stats, listed_presence=False)
                stats["fetched"] += 1
                stats["changed"] += int(changed)
            except Exception:
                stats["failed"] += 1
                await self.session.rollback()
        await self.session.commit()

    async def _enqueue_parse_jobs(self, run_id: uuid.UUID, notice_id: uuid.UUID,
                                  version_id: uuid.UUID) -> None:
        await self.session.execute(text("""
            INSERT INTO inha_policy.crawl_jobs
              (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
            VALUES (:run_id, 'parse_document', :key, :notice_id, :version_id,
                    CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING
        """), {"run_id": run_id, "key": f"body:{version_id}", "notice_id": notice_id,
                 "version_id": version_id, "payload": json.dumps({"origin": "html_body"})})
        assets = (await self.session.execute(text("""
            SELECT id FROM inha_policy.notice_version_assets
            WHERE notice_version_id=:version_id AND download_status='succeeded'
        """), {"version_id": version_id})).scalars().all()
        for asset_id in assets:
            await self.session.execute(text("""
                INSERT INTO inha_policy.crawl_jobs
                  (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
                VALUES (:run_id, 'parse_document', :key, :notice_id, :version_id,
                        CAST(:payload AS jsonb)) ON CONFLICT DO NOTHING
            """), {"run_id": run_id, "key": f"asset:{asset_id}", "notice_id": notice_id,
                     "version_id": version_id,
                     "payload": json.dumps({"origin": "asset", "asset_occurrence_id": str(asset_id)})})

    async def _queue_review(self, kind: str, entity_type: str, entity_id: uuid.UUID,
                            payload: dict[str, Any]) -> None:
        await self.session.execute(text("""
            INSERT INTO inha_policy.review_items
              (review_kind, entity_type, entity_id, payload)
            VALUES (:kind, :entity_type, :entity_id, CAST(:payload AS jsonb))
        """), {"kind": kind, "entity_type": entity_type, "entity_id": entity_id,
                 "payload": json.dumps(payload, ensure_ascii=False)})
