"""Readable admin pages: dashboard, scholarship list, notice list and notice detail.

Actions keep living in ``admin.py`` (POST endpoints); these pages only read and link to them.
"""
from __future__ import annotations

import base64
import json
import uuid
from datetime import date, datetime
from html import escape as html_escape
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import text

from .admin import Session, Viewer, _context, router, templates
from .admin_text import LABELS
from .config import get_settings
from .storage import build_object_store

PAGE_SIZE = 50
SEOUL = ZoneInfo("Asia/Seoul")


def today() -> date:
    return datetime.now(SEOUL).date()


def won(value: Any) -> str:
    if value is None:
        return ""
    amount = int(value)
    if amount >= 10000 and amount % 10000 == 0:
        return f"{amount // 10000:,}만원"
    return f"{amount:,}원"


templates.env.filters["won"] = won


def benefit_label(row: dict[str, Any]) -> str:
    if row.get("percentage") is not None:
        return f"등록금 {float(row['percentage']):g}%"
    low, high = row.get("amount_min"), row.get("amount_max")
    if low is not None and high is not None and low != high:
        return f"{won(low)}~{won(high)}"
    if high is not None:
        return won(high) if low is not None else f"최대 {won(high)}"
    return ""


def deadline_state(next_deadline: date | None, last_deadline: date | None) -> dict[str, Any]:
    if next_deadline is not None:
        days = (next_deadline - today()).days
        return {"kind": "warn" if days <= 7 else "ok", "label": "오늘 마감" if days == 0 else f"D-{days}",
                "date": next_deadline}
    if last_deadline is not None:
        return {"kind": "", "label": "마감", "date": last_deadline}
    return {"kind": "", "label": "기한 미상", "date": None}


@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session, admin: Viewer) -> HTMLResponse:
    funnel = dict((await session.execute(text("""
        SELECT
          (SELECT count(*) FROM inha_policy.notices WHERE scope_status='included') AS notices,
          (SELECT count(*) FROM inha_policy.notices WHERE scope_status<>'included') AS excluded,
          (SELECT count(DISTINCT n.id) FROM inha_policy.notices n
             JOIN inha_policy.documents d ON d.notice_version_id=n.current_notice_version_id
            WHERE n.scope_status='included'
              AND NOT EXISTS (SELECT 1 FROM inha_policy.crawl_jobs j WHERE j.notice_version_id=n.current_notice_version_id
                              AND j.stage='parse_document' AND j.status IN ('queued','retry','running'))) AS parsed,
          (SELECT count(DISTINCT n.id) FROM inha_policy.notices n
             JOIN inha_policy.extraction_runs er ON er.notice_version_id=n.current_notice_version_id
            WHERE n.scope_status='included' AND er.status='succeeded') AS extracted,
          (SELECT count(*) FROM inha_policy.opportunities WHERE lifecycle_status <> 'merged') AS opportunities,
          (SELECT count(*) FROM inha_policy.opportunities WHERE current_version_id IS NOT NULL) AS published
    """))).mappings().one())
    queue = {row["stage"]: row for row in (await session.execute(text("""
        SELECT stage,
               count(*) FILTER (WHERE status IN ('queued','retry')) AS waiting,
               count(*) FILTER (WHERE status='running') AS running,
               count(*) FILTER (WHERE status='failed') AS failed
        FROM inha_policy.crawl_jobs GROUP BY stage
    """))).mappings()}
    reviews = (await session.execute(text("""
        SELECT review_kind, count(*) AS count FROM inha_policy.review_items
        WHERE status IN ('open','in_review') GROUP BY review_kind ORDER BY count DESC
    """))).mappings().all()
    closing = await _opportunity_rows(session, {"deadline": "open", "sort": "deadline"}, limit=8, offset=0)
    runs = (await session.execute(text("""
        SELECT r.id, r.mode, r.status, r.started_at, r.finished_at, r.discovered_count,
               r.changed_count, r.failed_count
        FROM inha_policy.crawl_runs r ORDER BY r.scheduled_for DESC LIMIT 5
    """))).mappings().all()
    ai_pending = (await session.execute(text(
        "SELECT count(*) FROM inha_policy.review_items WHERE status='ai_pending'"))).scalar_one()
    return templates.TemplateResponse(request, "dashboard.html", _context(
        request, admin, funnel=funnel, queue=queue, reviews=reviews, closing=closing[0], runs=runs,
        review_labels=REVIEW_LABELS, ai_pending=ai_pending))


REVIEW_LABELS = {kind: words for kind, (words, _tone) in LABELS["review_kind"].items()}


async def _opportunity_rows(session, filters: dict[str, str], limit: int, offset: int
                            ) -> tuple[list[dict[str, Any]], int]:
    where = ["o.lifecycle_status <> 'merged'"]
    params: dict[str, Any] = {"limit": limit, "offset": offset, "today": today()}
    if filters.get("q"):
        where.append("(l.title ILIKE :q OR coalesce(l.provider_name,'') ILIKE :q OR coalesce(s.notice_title,'') ILIKE :q)")
        params["q"] = f"%{filters['q']}%"
    if filters.get("state") == "live":
        where.append("o.current_version_id IS NOT NULL")
    elif filters.get("state") == "draft":
        where.append("o.current_version_id IS NULL")
    if filters.get("quality") in {"complete", "partial", "needs_review"}:
        where.append("l.data_quality_status = :quality")
        params["quality"] = filters["quality"]
    deadline = filters.get("deadline")
    if deadline == "open":
        where.append("w.next_deadline IS NOT NULL")
    elif deadline == "closing":
        where.append("w.next_deadline BETWEEN :today AND :today + 14")
    elif deadline == "closed":
        where.append("w.next_deadline IS NULL AND w.last_deadline IS NOT NULL")
    elif deadline == "unknown":
        where.append("w.last_deadline IS NULL")
    order = ("w.next_deadline NULLS LAST, w.last_deadline DESC NULLS LAST" if filters.get("sort") == "deadline"
             else "l.created_at DESC")
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (ov.opportunity_id) ov.*
            FROM inha_policy.opportunity_versions ov
            ORDER BY ov.opportunity_id, ov.version_no DESC
        )
        SELECT l.opportunity_id, l.id AS version_id, l.version_no, l.title, l.provider_name,
               l.academic_year, l.academic_term, l.data_quality_status, l.publication_state,
               l.selection_capacity, l.selection_capacity_scope, l.created_at, l.edit_kind,
               o.current_version_id IS NOT NULL AS live, w.next_deadline, w.last_deadline,
               b.amount_min, b.amount_max, b.percentage, b.payment_frequency,
               s.notice_id, s.notice_title, s.source_count,
               (SELECT count(*) FROM inha_policy.review_items r
                 WHERE r.opportunity_id=o.id AND r.status IN ('open','in_review')) AS open_reviews,
               count(*) OVER () AS total
        FROM latest l
        JOIN inha_policy.opportunities o ON o.id=l.opportunity_id
        LEFT JOIN LATERAL (
            SELECT min(end_date) FILTER (WHERE end_date >= :today) AS next_deadline, max(end_date) AS last_deadline
            FROM inha_policy.application_windows
            WHERE opportunity_version_id=l.id
              AND window_kind IN ('application', 'additional_application', 'nomination')
        ) w ON true
        LEFT JOIN LATERAL (
            SELECT amount_min, amount_max, percentage, payment_frequency FROM inha_policy.benefits
            WHERE opportunity_version_id=l.id ORDER BY amount_max DESC NULLS LAST, benefit_key LIMIT 1
        ) b ON true
        LEFT JOIN LATERAL (
            SELECT n.id AS notice_id, nv.title AS notice_title, count(*) OVER () AS source_count
            FROM inha_policy.opportunity_version_sources src
            JOIN inha_policy.notice_versions nv ON nv.id=src.notice_version_id
            JOIN inha_policy.notices n ON n.id=nv.notice_id
            WHERE src.opportunity_version_id=l.id ORDER BY nv.observed_at LIMIT 1
        ) s ON true
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT :limit OFFSET :offset
    """
    rows = [dict(row) for row in (await session.execute(text(sql), params)).mappings()]
    for row in rows:
        row["deadline"] = deadline_state(row["next_deadline"], row["last_deadline"])
        row["benefit"] = benefit_label(row)
    return rows, (rows[0]["total"] if rows else 0)


@router.get("/opportunities", response_class=HTMLResponse)
async def opportunity_list(request: Request, session: Session, admin: Viewer) -> HTMLResponse:
    filters = {key: request.query_params.get(key, "") for key in ("q", "state", "quality", "deadline", "sort")}
    filters["sort"] = filters["sort"] or "deadline"
    page = max(1, int(request.query_params.get("page", "1") or 1))
    rows, total = await _opportunity_rows(session, filters, PAGE_SIZE, (page - 1) * PAGE_SIZE)
    return templates.TemplateResponse(request, "opportunities.html", _context(
        request, admin, rows=rows, total=total, filters=filters, page=page,
        pages=max(1, -(-total // PAGE_SIZE))))


@router.get("/notices", response_class=HTMLResponse)
async def notice_list(request: Request, session: Session, admin: Viewer) -> HTMLResponse:
    q = request.query_params.get("q", "")
    scope = request.query_params.get("scope", "all")
    stage = request.query_params.get("stage", "")
    page = max(1, int(request.query_params.get("page", "1") or 1))
    where = ["true"]
    params: dict[str, Any] = {"limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE}
    if scope in {"included", "excluded", "uncertain"}:
        where.append("n.scope_status=:scope")
        params["scope"] = scope
    if q:
        where.append("nv.title ILIKE :q")
        params["q"] = f"%{q}%"
    stage_filter = {
        "parsing": "p.pending > 0",
        "extract_wait": "p.pending = 0 AND coalesce(x.status,'') <> 'succeeded'",
        "failed": "(p.failed > 0 OR x.status = 'failed')",
        "done": "x.status = 'succeeded'",
    }.get(stage)
    if stage_filter:
        where.append(stage_filter)
    rows = (await session.execute(text(f"""
        SELECT n.id, n.canonical_url, n.scope_status, n.availability_status, n.last_checked_at,
               nv.id AS version_id, nv.title, nv.published_on, nv.category_name, nv.revision_no,
               (SELECT count(*) FROM inha_policy.notice_version_assets a
                 WHERE a.notice_version_id=nv.id AND a.role='attachment') AS attachments,
               p.pending, p.failed, x.status AS extraction_status, x.prompt_version,
               (SELECT count(DISTINCT ov.opportunity_id) FROM inha_policy.opportunity_version_sources s
                  JOIN inha_policy.opportunity_versions ov ON ov.id=s.opportunity_version_id
                 WHERE s.notice_version_id=nv.id) AS opportunities,
               count(*) OVER () AS total
        FROM inha_policy.notices n
        JOIN inha_policy.notice_versions nv ON nv.id=n.current_notice_version_id
        LEFT JOIN LATERAL (
            SELECT count(*) FILTER (WHERE status IN ('queued','retry','running')) AS pending,
                   count(*) FILTER (WHERE status='failed') AS failed
            FROM inha_policy.crawl_jobs WHERE notice_version_id=nv.id AND stage='parse_document'
        ) p ON true
        LEFT JOIN LATERAL (
            SELECT status, prompt_version FROM inha_policy.extraction_runs
            WHERE notice_version_id=nv.id ORDER BY started_at DESC LIMIT 1
        ) x ON true
        WHERE {' AND '.join(where)}
        ORDER BY nv.published_on DESC NULLS LAST, n.external_post_id DESC
        LIMIT :limit OFFSET :offset
    """), params)).mappings().all()
    total = rows[0]["total"] if rows else 0
    return templates.TemplateResponse(request, "notices.html", _context(
        request, admin, rows=rows, total=total, q=q, scope=scope, stage=stage, page=page,
        pages=max(1, -(-total // PAGE_SIZE))))


@router.get("/notices/{notice_id}", response_class=HTMLResponse)
async def notice_detail(notice_id: uuid.UUID, request: Request, session: Session,
                        admin: Viewer) -> HTMLResponse:
    notice = (await session.execute(text("""
        SELECT n.*, nv.id AS version_id, nv.title, nv.published_on, nv.category_name, nv.department_name,
               nv.author_name, nv.revision_no, nv.observed_at, nv.asset_collection_status
        FROM inha_policy.notices n JOIN inha_policy.notice_versions nv ON nv.id=n.current_notice_version_id
        WHERE n.id=:id
    """), {"id": notice_id})).mappings().one_or_none()
    if notice is None:
        raise HTTPException(404)
    version_id = notice["version_id"]
    versions = (await session.execute(text("""
        SELECT id, revision_no, title, observed_at, content_fingerprint FROM inha_policy.notice_versions
        WHERE notice_id=:id ORDER BY revision_no DESC
    """), {"id": notice_id})).mappings().all()
    documents = (await session.execute(text("""
        SELECT DISTINCT ON (d.asset_occurrence_id) d.id, d.origin_kind, d.parser_name, d.parser_version,
               d.status, d.quality_flags, d.finished_at, nva.original_filename, nva.role,
               nva.binary_asset_id, ba.detected_mime, ba.byte_size,
               (SELECT count(*) FROM inha_policy.document_blocks b WHERE b.document_id=d.id) AS blocks
        FROM inha_policy.documents d
        LEFT JOIN inha_policy.notice_version_assets nva ON nva.id=d.asset_occurrence_id
        LEFT JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
        WHERE d.notice_version_id=:version AND d.sealed_at IS NOT NULL
        ORDER BY d.asset_occurrence_id NULLS FIRST, d.finished_at DESC
    """), {"version": version_id})).mappings().all()
    assets = (await session.execute(text("""
        SELECT nva.id, nva.role, nva.original_filename, nva.download_status, nva.binary_asset_id
        FROM inha_policy.notice_version_assets nva WHERE nva.notice_version_id=:version ORDER BY nva.ordinal
    """), {"version": version_id})).mappings().all()
    opportunities = (await session.execute(text("""
        SELECT DISTINCT ON (ov.opportunity_id) ov.opportunity_id, ov.title, ov.version_no,
               ov.data_quality_status, ov.publication_state, s.relation_kind
        FROM inha_policy.opportunity_version_sources s
        JOIN inha_policy.opportunity_versions ov ON ov.id=s.opportunity_version_id
        WHERE s.notice_version_id=:version ORDER BY ov.opportunity_id, ov.version_no DESC
    """), {"version": version_id})).mappings().all()
    runs = (await session.execute(text("""
        SELECT id, status, prompt_version, model_id, input_tokens, output_tokens, error_code,
               left(error_message, 300) AS error_message, started_at, finished_at,
               jsonb_array_length(coalesce(parsed_output->'opportunities','[]'::jsonb)) AS item_count
        FROM inha_policy.extraction_runs WHERE notice_version_id=:version ORDER BY started_at DESC
    """), {"version": version_id})).mappings().all()
    jobs = (await session.execute(text("""
        SELECT id, stage, status, attempt_count, max_attempts, available_at, error_code,
               left(error_message, 1000) AS error_message, created_at, started_at, locked_at,
               finished_at, result_metadata
        FROM inha_policy.crawl_jobs WHERE notice_version_id=:version ORDER BY created_at DESC LIMIT 60
    """), {"version": version_id})).mappings().all()
    body = (await session.execute(text("""
        SELECT parsed_text FROM inha_policy.documents
        WHERE notice_version_id=:version AND origin_kind='html_body' AND sealed_at IS NOT NULL
        ORDER BY finished_at DESC LIMIT 1
    """), {"version": version_id})).scalar_one_or_none()
    exchanges = (await session.execute(text("""
        SELECT id, purpose, model_id, status, latency_ms, input_tokens, output_tokens, started_at,
               extraction_run_id, left(error_message, 200) AS error_message
        FROM inha_policy.llm_exchanges WHERE notice_version_id=:version ORDER BY started_at DESC LIMIT 100
    """), {"version": version_id})).mappings().all()
    reviews = (await session.execute(text("""
        SELECT r.id, r.review_kind, r.status, r.opportunity_id, r.created_at, r.resolution_note
        FROM inha_policy.review_items r
        WHERE r.entity_id IN (:notice, :version)
           OR r.opportunity_id IN (SELECT ov.opportunity_id FROM inha_policy.opportunity_version_sources s
                                   JOIN inha_policy.opportunity_versions ov ON ov.id=s.opportunity_version_id
                                   WHERE s.notice_version_id=:version)
        ORDER BY (r.status IN ('open', 'in_review', 'ai_pending')) DESC, r.created_at DESC LIMIT 30
    """), {"notice": notice_id, "version": version_id})).mappings().all()
    return templates.TemplateResponse(request, "notice.html", _context(
        request, admin, notice=notice, versions=versions, documents=documents, assets=assets,
        opportunities=opportunities, runs=runs, jobs=jobs, body=body, reviews=reviews, exchanges=exchanges,
        review_labels=REVIEW_LABELS))


@router.get("/llm-exchanges/{exchange_id}", response_class=HTMLResponse)
async def llm_exchange(exchange_id: uuid.UUID, request: Request, session: Session,
                       admin: Viewer) -> HTMLResponse:
    """One provider call exactly as sent and received."""
    row = (await session.execute(text("""
        SELECT x.*, nv.notice_id, nv.title FROM inha_policy.llm_exchanges x
        LEFT JOIN inha_policy.notice_versions nv ON nv.id=x.notice_version_id WHERE x.id=:id
    """), {"id": exchange_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    store = build_object_store(get_settings())

    def body(key: str | None) -> str | None:
        if key is None:
            return None
        raw = store.get(key).decode()
        try:
            return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except ValueError:
            return raw

    return templates.TemplateResponse(request, "llm_exchange.html", _context(
        request, admin, exchange=row, request_body=body(row["request_storage_key"]),
        response_body=body(row["response_storage_key"])))


@router.get("/llm-exchanges/{exchange_id}/{part}.json")
async def llm_exchange_raw(exchange_id: uuid.UUID, part: str, session: Session, admin: Viewer) -> Response:
    if part not in {"request", "response"}:
        raise HTTPException(404)
    key = (await session.execute(text(f"""
        SELECT {part}_storage_key FROM inha_policy.llm_exchanges WHERE id=:id
    """), {"id": exchange_id})).scalar_one_or_none()
    if key is None:
        raise HTTPException(404)
    return Response(build_object_store(get_settings()).get(key), media_type="application/json",
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


INLINE_MIME = ("application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp")


@router.get("/assets/{asset_id}/view")
async def asset_view(asset_id: uuid.UUID, session: Session, admin: Viewer) -> Response:
    """Serve a stored PDF or image inline so the document viewer can show the original."""
    row = (await session.execute(text("""
        SELECT storage_key, detected_mime FROM inha_policy.binary_assets WHERE id=:id
    """), {"id": asset_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    storage_key, mime = row["storage_key"], row["detected_mime"].split(";")[0]
    if not row["detected_mime"].startswith(INLINE_MIME):
        # HWP and other formats browsers cannot show are previewed through their PDF rendering.
        rendered = (await session.execute(text("""
            SELECT storage_key FROM inha_policy.derived_files
            WHERE binary_asset_id=:id AND kind='pdf_render' ORDER BY created_at DESC LIMIT 1
        """), {"id": asset_id})).scalar_one_or_none()
        if rendered is None:
            raise HTTPException(415, "이 파일 형식은 브라우저에서 바로 볼 수 없습니다. 원본을 내려받아 확인하세요.")
        storage_key, mime = rendered, "application/pdf"
    payload = build_object_store(get_settings()).get(storage_key)
    return Response(payload, media_type=mime, headers={
        "Content-Disposition": "inline", "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": "default-src 'none'; img-src 'self'; frame-ancestors 'self'"})


@router.get("/documents/{document_id}/source")
async def document_source(document_id: uuid.UUID, session: Session, admin: Viewer) -> Response:
    """The notice body exactly as collected, isolated from the admin origin.

    The CSP sandbox gives the page an opaque origin with scripts disabled, so collected HTML can
    neither run code nor reach the admin session. Inline images use the stored copies.
    """
    row = (await session.execute(text("""
        SELECT nv.id, nv.body_html, n.canonical_url FROM inha_policy.documents d
        JOIN inha_policy.notice_versions nv ON nv.id=d.notice_version_id
        JOIN inha_policy.notices n ON n.id=nv.notice_id
        WHERE d.id=:id AND d.origin_kind='html_body'
    """), {"id": document_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    body = row["body_html"] or ""
    images = (await session.execute(text("""
        SELECT nva.original_url, nva.resolved_url, ba.storage_key, ba.detected_mime
        FROM inha_policy.notice_version_assets nva
        JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
        WHERE nva.notice_version_id=:version AND nva.role='inline_image'
    """), {"version": row["id"]})).mappings().all()
    store = build_object_store(get_settings())
    for image in images:
        data = base64.b64encode(store.get(image["storage_key"])).decode()
        uri = f"data:{image['detected_mime'].split(';')[0]};base64,{data}"
        for url in {image["original_url"], image["resolved_url"]} - {None}:
            path = urlsplit(url)._replace(scheme="", netloc="").geturl()
            body = body.replace(f'"{url}"', f'"{uri}"').replace(f'"{path}"', f'"{uri}"')
    page = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            f'<base href="{html_escape(row["canonical_url"])}" target="_blank">'
            '<style>body{font:15px/1.6 "Noto Sans KR",sans-serif;margin:16px;color:#222}'
            'img{max-width:100%;height:auto}table{border-collapse:collapse;max-width:100%}'
            'td,th{border:1px solid #ccc;padding:4px}</style></head>'
            f'<body>{body}</body></html>')
    return HTMLResponse(page, headers={
        "Content-Security-Policy": ("sandbox; default-src 'none'; img-src data: https: http:; "
                                    "style-src 'unsafe-inline'; font-src data: https:; "
                                    "frame-ancestors 'self'"),
        "X-Frame-Options": "SAMEORIGIN",
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
