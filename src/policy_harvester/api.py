from __future__ import annotations

import base64
import json
import math
import os
import time as monotonic_time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .ai.providers import CapabilityError, ProviderRegistry
from .apikeys import api_key_header, verify_api_key
from .config import effective_configuration, get_settings, resolved_settings
from .db import engine, get_session
from .search import understand_query


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await engine.dispose()


API_DESCRIPTION = """인하대 장학 공고에서 구조화한 장학 정보를 조회하는 API입니다.

모든 `/v1` 요청에는 관리자 화면의 **API key** 메뉴에서 발급한 key를 `X-API-Key` header로 보냅니다.
이 화면에서는 오른쪽 위 **Authorize**에 key를 넣으면 "Try it out" 요청에 붙습니다.
key마다 분당 요청 한도가 있고, 넘으면 429와 `Retry-After`를 돌려줍니다.
"""

app = FastAPI(
    title="Policy Harvester API",
    version="1.0.0",
    description=API_DESCRIPTION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)
Session = Annotated[AsyncSession, Depends(get_session)]


async def require_api_key(session: Session,
                          key: Annotated[str | None, Security(api_key_header)]) -> None:
    await verify_api_key(session, key)


v1 = APIRouter(dependencies=[Depends(require_api_key)],
               responses={401: {"description": "X-API-Key가 없거나 유효하지 않음"},
                          429: {"description": "요청 한도 초과"}})
REQUEST_WINDOWS: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def security_and_rate_limit(request: Request, call_next):
    address = request.client.host if request.client else "unknown"
    now = monotonic_time.monotonic()
    window = REQUEST_WINDOWS[address]
    while window and window[0] < now - 60:
        window.popleft()
    limit = 300 if request.url.path.startswith("/admin") else 180
    if len(window) >= limit:
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": "60"})
    window.append(now)
    response = await call_next(request)
    # Routes that must be framed by the admin document viewer set their own policy.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/admin"):
        # Admin pages carry session state; never let a browser or proxy serve a stale copy
        # (a CDN-cached user list after creating a user looked like the creation had failed).
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path.startswith("/v1"):
        # A shared cache would answer requests without checking the API key or its rate limit.
        response.headers.setdefault("Cache-Control", "private, no-store")
    response.headers.setdefault("Content-Security-Policy", (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "form-action 'self'; frame-ancestors 'none'"
    ))
    return response


STUDENT_WINDOW_KINDS = ("application", "additional_application")


def application_status(windows: list[Any], override: str | None, now: datetime) -> str:
    """Status computed from the stored windows and the current Seoul time, never from the model.

    "active": an application window has started and not ended (or closes on a non-date rule),
    "upcoming": the next one starts later, "closed": every dated one has ended, "unknown": no dates.
    The source's own cancellation/suspension wins.
    """
    if override:
        return {"closed_by_source": "closed"}.get(override, override)
    rows = [row for row in windows if row["window_kind"] in STUDENT_WINDOW_KINDS] or list(windows)
    today, clock = now.date(), now.time().replace(tzinfo=None)

    def started(row: Any) -> bool:
        return row["start_date"] is None or row["start_date"] < today or (
            row["start_date"] == today and (row["start_time"] is None or row["start_time"] <= clock))

    def ended(row: Any) -> bool:
        return row["end_date"] is not None and (row["end_date"] < today or (
            row["end_date"] == today and row["end_time"] is not None and row["end_time"] < clock))

    dated = [row for row in rows if row["start_date"] or row["end_date"]]
    if any(started(row) and not ended(row) and (row["end_date"] or row["closing_rule"] != "fixed")
           for row in dated):
        return "active"
    if any(row["start_date"] and not started(row) for row in dated):
        return "upcoming"
    if dated and all(ended(row) for row in dated if row["end_date"]) and any(row["end_date"] for row in dated):
        return "closed"
    return "unknown"


def _selection_details(selection: dict[str, Any] | None) -> dict[str, Any] | None:
    """Selection facts under the schema v2 names, also for versions extracted with v1."""
    if not selection or "selection_count" in selection:
        return selection
    renamed = {"final_selection_count": "selection_count", "university_nomination_count": "nomination_quota"}
    result = {renamed.get(key, key): value for key, value in selection.items()
              if key not in {"recruitment_count", "result_date"}}
    if result.get("selection_count", {}).get("state") not in {"stated", "resolved_update"}:
        result["selection_count"] = selection.get("recruitment_count") or result.get("selection_count")
    return result


async def _query_vector(session: AsyncSession, q: str) -> str:
    """The query embedded with the active profile, normalized, as a pgvector literal."""
    profile = (await session.execute(text("""
        SELECT model_id, dimensions, config FROM inha_policy.embedding_profiles WHERE is_active LIMIT 1
    """))).mappings().one_or_none()
    if profile is None:
        raise LookupError("no active embedding profile (run the embed stage first)")
    provider_name = profile["config"].get("provider", "openai")
    vector = (await ProviderRegistry(await resolved_settings(session)).embedding(provider_name).embed(
        model=profile["model_id"], texts=[q], dimensions=profile["dimensions"], kind="query"))[0]
    magnitude = math.sqrt(sum(value * value for value in vector))
    if len(vector) != profile["dimensions"] or magnitude == 0:
        raise ValueError("query embedding has the wrong dimensions or is a zero vector")
    return "[" + ",".join(str(value / magnitude) for value in vector) + "]"


def _cursor_encode(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode().rstrip("=")


def _cursor_decode(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        return max(0, int(value["offset"]))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, time, uuid.UUID)):
        return value.isoformat() if not isinstance(value, uuid.UUID) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


SWAGGER_DIR = Path(os.environ.get("SWAGGER_UI_DIR", "/opt/swagger-ui"))
SWAGGER_ASSETS = {"swagger-ui.css": "text/css", "swagger-ui-bundle.js": "text/javascript",
                  "favicon-32x32.png": "image/png"}
SWAGGER_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>Policy Harvester API</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/docs/assets/swagger-ui.css"><link rel="icon" href="/docs/assets/favicon-32x32.png">
</head><body><div id="swagger-ui"></div>
<script src="/docs/assets/swagger-ui-bundle.js"></script><script src="/docs/init.js"></script>
</body></html>"""
# A file, not an inline script, so the default CSP (script-src 'self') applies unchanged.
SWAGGER_INIT = """window.ui = SwaggerUIBundle({
  url: "/openapi.json", dom_id: "#swagger-ui", deepLinking: true,
  persistAuthorization: true, displayRequestDuration: true, tryItOutEnabled: true
});"""


@app.get("/docs", include_in_schema=False)
async def swagger_ui() -> HTMLResponse:
    if not (SWAGGER_DIR / "swagger-ui-bundle.js").exists():
        return HTMLResponse("Swagger UI assets are not installed (SWAGGER_UI_DIR); "
                            "see /openapi.json for the API description.", status_code=503)
    return HTMLResponse(SWAGGER_PAGE)


@app.get("/docs/init.js", include_in_schema=False)
async def swagger_init() -> Response:
    return Response(SWAGGER_INIT, media_type="text/javascript")


@app.get("/docs/assets/{name}", include_in_schema=False)
async def swagger_asset(name: str) -> FileResponse:
    if name not in SWAGGER_ASSETS or not (SWAGGER_DIR / name).exists():
        raise HTTPException(404)
    return FileResponse(SWAGGER_DIR / name, media_type=SWAGGER_ASSETS[name],
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/healthz", include_in_schema=False)
async def health(session: Session) -> dict[str, Any]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@v1.get("/v1/opportunities")
async def list_opportunities(
    session: Session,
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    organization: str | None = None,
    application_start: date | None = None,
    application_end: date | None = None,
    benefit_type: str | None = None,
    min_amount: int | None = Query(default=None, ge=0),
    max_amount: int | None = Query(default=None, ge=0),
    student_type: str | None = None,
    region: str | None = None,
    search_mode: Literal["hybrid", "lexical", "vector"] = Query(
        default="hybrid", description="q의 순위: hybrid(어휘 0.45 + 의미 0.55), lexical(어휘만), vector(의미만)"),
    income_bracket_max: int | None = Query(default=None, ge=0, le=20),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    inferred = understand_query(q or "")
    status = status or inferred.get("status")
    benefit_type = benefit_type or inferred.get("benefit_type")
    min_amount = min_amount if min_amount is not None else inferred.get("min_amount")
    student_type = student_type or inferred.get("student_type")
    region = region or inferred.get("region")
    income_bracket_max = (income_bracket_max if income_bracket_max is not None
                          else inferred.get("income_bracket_max"))
    end_from = inferred.get("application_end_from")
    end_to = application_end or inferred.get("application_end_to")
    offset = _cursor_decode(cursor)
    korea_now = datetime.now(ZoneInfo("Asia/Seoul"))
    parameters: dict[str, Any] = {"limit": limit + 1, "offset": offset,
                                  "today": korea_now.date(), "now_time": korea_now.time()}
    predicates = ["NOT ov.source_is_stale"]
    if category:
        predicates.append(":category = ANY(ov.categories)")
        parameters["category"] = category
    if organization:
        predicates.append("ov.provider_name ILIKE '%' || :organization || '%'")
        parameters["organization"] = organization
    if application_start:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id AND w.start_date >= :application_start)")
        parameters["application_start"] = application_start
    if end_from:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id AND w.end_date >= :end_from)")
        parameters["end_from"] = end_from
    if end_to:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id AND w.end_date <= :application_end)")
        parameters["application_end"] = end_to
    if benefit_type:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.benefits b WHERE b.opportunity_version_id=ov.id AND b.benefit_kind=:benefit_type)")
        parameters["benefit_type"] = benefit_type if benefit_type != "housing" else "service"
    if min_amount is not None:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.benefits b WHERE b.opportunity_version_id=ov.id AND b.amount_max >= :min_amount)")
        parameters["min_amount"] = min_amount
    if max_amount is not None:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.benefits b WHERE b.opportunity_version_id=ov.id AND b.amount_min <= :max_amount)")
        parameters["max_amount"] = max_amount
    if student_type:
        # "Can a graduate student apply?": unrestricted or unknown profiles stay in the result.
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.eligibility_profiles e WHERE e.opportunity_version_id=ov.id AND (e.academic_level_rule_status <> 'restricted' OR :student_type = ANY(e.academic_levels)))")
        parameters["student_type"] = student_type
    if region:
        # Stored regions are free text ("서울특별시", "광산구"); a 시·도 prefix matches either way.
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.eligibility_profiles e WHERE e.opportunity_version_id=ov.id AND (e.residency_rule_status <> 'restricted' OR EXISTS (SELECT 1 FROM unnest(e.residency_region_codes) r WHERE r LIKE :region || '%' OR :region LIKE r || '%')))")
        parameters["region"] = region
    if income_bracket_max is not None:
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.eligibility_profiles e WHERE e.opportunity_version_id=ov.id AND e.income_projection_is_exact AND e.income_metric='kosaf_support_bracket' AND e.income_max <= :income_bracket_max)")
        parameters["income_bracket_max"] = income_bracket_max
    if status == "active":
        predicates.append("ov.source_status_override IS NULL AND EXISTS (SELECT 1 FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id AND w.confirmation_status='confirmed' AND (w.start_date IS NULL OR w.start_date < :today OR (w.start_date=:today AND (w.start_time IS NULL OR w.start_time <= :now_time))) AND ((w.closing_rule='fixed' AND (w.end_date > :today OR (w.end_date=:today AND (w.end_time IS NULL OR w.end_time >= :now_time)))) OR w.closing_rule IN ('rolling','until_budget_exhausted','until_filled')))")
    elif status == "closed":
        predicates.append("EXISTS (SELECT 1 FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id AND w.end_date < :today)")
    elif status == "cancelled":
        predicates.append("ov.source_status_override='cancelled'")

    vector_literal = None
    vector_error = None
    if q:
        parameters["q"] = q
        if search_mode != "lexical":
            try:
                vector_literal = await _query_vector(session, q)
                parameters["query_vector"] = vector_literal
            except Exception as exc:  # reported in the response; hybrid falls back to lexical
                vector_error = f"{exc.__class__.__name__}: {exc}"[:300]
            if search_mode == "vector" and vector_literal is None:
                raise HTTPException(503, f"vector search is unavailable: {vector_error}")
    where = " AND ".join(predicates)
    lexical = "0.0"
    vector = "0.0"
    chunk_join = ""
    if q and search_mode != "vector":
        lexical = "greatest(similarity(ov.title, :q), coalesce(max(ts_rank(sc.search_tsv, plainto_tsquery('simple', :q))), 0))"
    if q:
        chunk_join = "LEFT JOIN inha_policy.current_search_chunks sc ON sc.opportunity_version_id=ov.id"
        if vector_literal:
            vector = "coalesce(max(1 - (sc.embedding <=> CAST(:query_vector AS vector))), 0)"
    weights = {"hybrid": (0.45, 0.55), "lexical": (1.0, 0.0), "vector": (0.0, 1.0)}[search_mode]
    sql = text(f"""
        SELECT ov.id AS version_id, ov.opportunity_id, ov.version_no, ov.observed_at,
               ov.published_at, ov.title, ov.summary, ov.opportunity_kind, ov.categories,
               ov.provider_name, ov.academic_year, ov.academic_term,
               ov.source_status_override, ov.data_quality_status, ov.quality_flags,
               {lexical} AS lexical_score, {vector} AS vector_score
        FROM inha_policy.current_opportunity_versions ov
        {chunk_join}
        WHERE {where}
        GROUP BY ov.id, ov.opportunity_id, ov.version_no, ov.observed_at, ov.published_at,
                 ov.title, ov.summary, ov.opportunity_kind, ov.categories, ov.provider_name,
                 ov.academic_year, ov.academic_term, ov.source_status_override,
                 ov.data_quality_status, ov.quality_flags
        ORDER BY ({lexical}) * {weights[0]} + ({vector}) * {weights[1]} DESC, ov.published_at DESC, ov.opportunity_id
        LIMIT :limit OFFSET :offset
    """)
    rows = [dict(row) for row in (await session.execute(sql, parameters)).mappings().all()]
    has_more = len(rows) > limit
    rows = rows[:limit]
    return {
        "items": [_json_value(row) for row in rows],
        "next_cursor": _cursor_encode(offset + limit) if has_more else None,
        "query": {"text": q, "inferred_filters": _json_value(inferred), "search_mode": search_mode,
                  "vector_used": vector_literal is not None, "vector_error": vector_error},
    }


async def _manual_overrides(session: AsyncSession, opportunity_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (field_path, scope_key)
               field_path, scope_key, action, value_json, reason, created_at
        FROM inha_policy.manual_overrides WHERE opportunity_id=:id
        ORDER BY field_path, scope_key, created_at DESC
    """), {"id": opportunity_id})).mappings().all()
    return [dict(row) for row in rows]


def _apply_pointer(document: dict[str, Any], pointer: str, value: Any) -> bool:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    target: Any = document
    try:
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        if isinstance(target, list):
            target[int(parts[-1])] = value
        else:
            target[parts[-1]] = value
        return True
    except (KeyError, IndexError, ValueError, TypeError):
        return False


@v1.get("/v1/opportunities/{opportunity_id}")
async def get_opportunity(opportunity_id: uuid.UUID, session: Session) -> Response:
    lifecycle = (await session.execute(text("""
        SELECT lifecycle_status, merged_into_id FROM inha_policy.opportunities WHERE id=:id
    """), {"id": opportunity_id})).mappings().one_or_none()
    if lifecycle is None:
        raise HTTPException(404, "opportunity not found")
    if lifecycle["lifecycle_status"] == "merged":
        return RedirectResponse(f"/v1/opportunities/{lifecycle['merged_into_id']}", status_code=308)
    version = (await session.execute(text("""
        SELECT * FROM inha_policy.current_opportunity_versions WHERE opportunity_id=:id
    """), {"id": opportunity_id})).mappings().one_or_none()
    if version is None:
        raise HTTPException(404, "opportunity is not published")
    version_id = version["id"]
    windows = (await session.execute(text("""
        SELECT * FROM inha_policy.application_windows WHERE opportunity_version_id=:id
        ORDER BY start_date NULLS LAST, window_key
    """), {"id": version_id})).mappings().all()
    benefits = (await session.execute(text(
        "SELECT * FROM inha_policy.benefits WHERE opportunity_version_id=:id ORDER BY benefit_key"
    ), {"id": version_id})).mappings().all()
    eligibility = (await session.execute(text(
        "SELECT * FROM inha_policy.eligibility_profiles WHERE opportunity_version_id=:id"
    ), {"id": version_id})).mappings().one_or_none()
    sources = (await session.execute(text("""
        SELECT n.id AS notice_id, nv.id AS notice_version_id, n.canonical_url,
               nv.title, nv.revision_no, ovs.relation_kind, ovs.source_usage
        FROM inha_policy.opportunity_version_sources ovs
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        JOIN inha_policy.notices n ON n.id=nv.notice_id
        WHERE ovs.opportunity_version_id=:id ORDER BY nv.observed_at
    """), {"id": version_id})).mappings().all()
    attachments = (await session.execute(text("""
        SELECT nva.id, nva.role, nva.original_filename, nva.original_url,
               ba.id AS asset_id, ba.detected_mime, ba.byte_size, ba.sha256
        FROM inha_policy.opportunity_version_sources ovs
        JOIN inha_policy.notice_version_assets nva ON nva.notice_version_id=ovs.notice_version_id
        LEFT JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
        WHERE ovs.opportunity_version_id=:id ORDER BY nva.ordinal
    """), {"id": version_id})).mappings().all()
    evidence = (await session.execute(text("""
        SELECT id, field_path, scope_key, candidate_value, quote_text, assertion_kind,
               candidate_status, verification_status, block_id
        FROM inha_policy.field_evidence WHERE opportunity_version_id=:id
        ORDER BY field_path, id
    """), {"id": version_id})).mappings().all()
    result = {
        "id": opportunity_id, "current_version": version["version_no"],
        "last_updated_at": version["published_at"], "title": version["title"],
        "description": version["summary"],
        "status": application_status(windows, version["source_status_override"],
                                     datetime.now(ZoneInfo("Asia/Seoul"))),
        "category": version["categories"], "organization": version["provider_name"],
        "academic_year": version["academic_year"], "academic_term": version["academic_term"],
        "benefits": [dict(row) for row in benefits],
        "eligibility": dict(eligibility) if eligibility else None,
        "application_windows": [dict(row) for row in windows],
        "application_methods": version["additional_details"].get("application_methods", []),
        "contacts": version["contacts"], "required_documents": version["required_documents"],
        "selection": {"capacity": version["selection_capacity"],
                      "capacity_scope": version["selection_capacity_scope"],
                      "process": version["selection_process"],
                      "details": _selection_details(version["additional_details"].get("selection"))},
        "source_urls": [dict(row) for row in sources], "attachments": [dict(row) for row in attachments],
        "evidence": [dict(row) for row in evidence],
        "data_quality": {"state": version["data_quality_status"], "flags": version["quality_flags"],
                         "unresolved_fields": version["unresolved_field_paths"],
                         "source_is_stale": version["source_is_stale"]},
    }
    overrides = await _manual_overrides(session, opportunity_id)
    applied = []
    for override in overrides:
        if override["action"] == "set" and _apply_pointer(result, override["field_path"], override["value_json"]):
            applied.append(override)
    if applied:
        result["last_updated_at"] = max(
            [version["published_at"], *(item["created_at"] for item in applied if item["created_at"])]
        )
    result["manual_overrides"] = applied
    return JSONResponse(_json_value(result))


@v1.get("/v1/opportunities/{opportunity_id}/versions")
async def opportunity_versions(opportunity_id: uuid.UUID, session: Session) -> dict[str, Any]:
    rows = (await session.execute(text("""
        SELECT id, version_no, edit_kind, schema_version, title, summary,
               data_quality_status, observed_at, published_at
        FROM inha_policy.opportunity_versions
        WHERE opportunity_id=:id AND publication_state='published'
        ORDER BY version_no DESC
    """), {"id": opportunity_id})).mappings().all()
    return {"items": _json_value([dict(row) for row in rows])}


@v1.get("/v1/opportunities/{opportunity_id}/sources")
async def opportunity_sources(opportunity_id: uuid.UUID, session: Session) -> dict[str, Any]:
    rows = (await session.execute(text("""
        SELECT n.id AS notice_id, n.canonical_url, nv.id AS notice_version_id,
               nv.revision_no, nv.title, nv.published_on, ovs.relation_kind,
               ovs.source_usage, ovs.is_current_dependency
        FROM inha_policy.opportunities o
        JOIN inha_policy.opportunity_version_sources ovs ON ovs.opportunity_version_id=o.current_version_id
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        JOIN inha_policy.notices n ON n.id=nv.notice_id
        WHERE o.id=:id ORDER BY nv.published_on, nv.revision_no
    """), {"id": opportunity_id})).mappings().all()
    return {"items": _json_value([dict(row) for row in rows])}


@v1.get("/v1/notices/{notice_id}")
async def get_notice(notice_id: uuid.UUID, session: Session) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT n.id, n.external_post_id, n.canonical_url, n.first_seen_at, n.last_seen_at,
               n.last_checked_at, n.availability_status, n.scope_status,
               nv.id AS current_version_id, nv.revision_no, nv.title, nv.author_name,
               nv.category_name, nv.published_on, nv.source_modified_at,
               CASE WHEN coalesce((s.crawl_config->>'public_raw_text')::boolean, false)
                    THEN nv.body_text ELSE NULL END AS body_text,
               nv.asset_collection_status, nv.content_fingerprint
        FROM inha_policy.notices n JOIN inha_policy.sources s ON s.id=n.source_id
        LEFT JOIN inha_policy.notice_versions nv
          ON nv.id=n.current_notice_version_id WHERE n.id=:id
    """), {"id": notice_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "notice not found")
    return _json_value(dict(row))


@v1.get("/v1/notices/{notice_id}/versions")
async def notice_versions(notice_id: uuid.UUID, session: Session) -> dict[str, Any]:
    rows = (await session.execute(text("""
        SELECT id, revision_no, title, published_on, source_modified_at, content_fingerprint,
               asset_collection_status, observed_at, sealed_at
        FROM inha_policy.notice_versions WHERE notice_id=:id ORDER BY revision_no DESC
    """), {"id": notice_id})).mappings().all()
    return {"items": _json_value([dict(row) for row in rows])}


@v1.get("/v1/notices/{notice_id}/observations")
async def notice_observations(notice_id: uuid.UUID, session: Session,
                              cursor: int = Query(default=0, ge=0),
                              limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    exists = (await session.execute(text(
        "SELECT EXISTS (SELECT 1 FROM inha_policy.notices WHERE id=:id)"
    ), {"id": notice_id})).scalar_one()
    if not exists:
        raise HTTPException(404, "notice not found")
    rows = (await session.execute(text("""
        SELECT id, crawl_run_id, resource_kind, requested_url, final_url, outcome,
               http_status, response_body_asset_id, representation_asset_id,
               reuses_snapshot_id, etag, last_modified_header, error_code,
               fetch_started_at, fetched_at, observed_at
        FROM inha_policy.fetch_snapshots WHERE notice_id=:id
        ORDER BY observed_at DESC, id OFFSET :offset LIMIT :limit
    """), {"id": notice_id, "offset": cursor, "limit": limit + 1})).mappings().all()
    has_more = len(rows) > limit
    items = [dict(row) for row in rows[:limit]]
    return {"items": _json_value(items),
            "next_cursor": cursor + limit if has_more else None}


@v1.get("/v1/sources/{source_id}/health")
async def source_health(source_id: uuid.UUID, session: Session) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT s.id, s.source_key, s.name, s.enabled, s.last_successful_run_at,
               latest.id AS latest_run_id, latest.status AS latest_run_status,
               latest.started_at, latest.finished_at, latest.discovered_count,
               latest.changed_count, latest.failed_count, latest.metrics,
               (SELECT count(*) FROM inha_policy.crawl_jobs j
                JOIN inha_policy.crawl_runs r ON r.id=j.crawl_run_id
                WHERE r.source_id=s.id AND j.status IN ('queued','retry','running')) AS pending_jobs,
               (SELECT count(*) FROM inha_policy.notices n
                WHERE n.source_id=s.id AND n.availability_status<>'available') AS unavailable_notices
        FROM inha_policy.sources s
        LEFT JOIN LATERAL (
          SELECT * FROM inha_policy.crawl_runs r WHERE r.source_id=s.id
          ORDER BY r.scheduled_for DESC LIMIT 1
        ) latest ON true
        WHERE s.id=:id
    """), {"id": source_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "source not found")
    return _json_value(dict(row))


@v1.get("/v1/assets/{asset_id}")
async def get_asset(asset_id: uuid.UUID, session: Session) -> dict[str, Any]:
    row = (await session.execute(text("""
        SELECT id, sha256, detected_mime, byte_size, width_px, height_px, page_count,
               technical_metadata, created_at FROM inha_policy.binary_assets WHERE id=:id
    """), {"id": asset_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "asset not found")
    return _json_value(dict(row))


@v1.get("/v1/changes")
async def changes(session: Session, cursor: int = Query(default=0, ge=0),
                  limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    rows = (await session.execute(text("""
        SELECT * FROM inha_policy.published_change_feed WHERE event_seq > :cursor
        ORDER BY event_seq LIMIT :limit
    """), {"cursor": cursor, "limit": limit})).mappings().all()
    items = [dict(row) for row in rows]
    return {"items": _json_value(items),
            "next_cursor": items[-1]["event_seq"] if items else cursor}


@v1.get("/v1/codes")
async def codes() -> dict[str, Any]:
    return {
        "status": ["active", "upcoming", "closed", "unknown", "cancelled", "suspended",
                   "source_missing", "removed", "archived"],
        "publication_state": ["draft", "published", "rejected"],
        "opportunity_lifecycle": ["active", "inactive", "merged"],
        "quality": ["complete", "partial", "needs_review"],
        "quality_flags": ["field_conflict", "source_unavailable", "parser_failed",
                          "evidence_validation_warning"],
        "missing_value_state": ["stated", "explicitly_none", "not_found", "unknown",
                                "parsing_failed", "conflict", "resolved_update"],
        "benefit_type": ["tuition", "living_cost", "travel", "cash", "in_kind", "service", "other"],
        "student_type": ["elementary", "middle", "high", "undergraduate", "graduate", "other"],
    }


from .admin import router as admin_router  # noqa: E402
from . import admin_pages  # noqa: E402,F401 - registers the read-only admin pages
app.include_router(v1)
app.include_router(admin_router, include_in_schema=False)


def run() -> None:
    import uvicorn
    uvicorn.run("policy_harvester.api:app", host="0.0.0.0", port=8000)
