from __future__ import annotations

import hashlib
import difflib
import json
import math
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Annotated, Callable
from urllib.parse import quote, urlencode, urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import TypeAdapter, ValidationError
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import admin_text
from .admin_text import ERROR_PAGE_TITLES, with_message
from .ai.providers import ProviderRegistry
from .ai.schema import interpret_placeholder_counts
from .pipeline.revisions import (EvidenceInput, FieldPatch, RevisionError, RevisionService,
                                 VerifiedRevision)
from .config import RUNTIME_KEYS, Settings, effective_configuration, get_settings, resolved_settings
from .ai.onnx_embeddings import ONNX_MODELS
from .apikeys import generate_key
from .pipeline.embeddings import CHUNKER_VERSION, coverage, ensure_profile
from .db import get_session
from .security import encrypt_secret, mask_proxy_url, mask_secret, validate_proxy_url
from .storage import build_object_store

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# tojson escapes non-ASCII by default ("\uad11..."); Korean must stay readable. HTML-unsafe
# characters (<, >, &, ') are still escaped by Jinja's htmlsafe dumper.
templates.env.policies["json.dumps_kwargs"] = {"ensure_ascii": False, "sort_keys": False}

COLUMN_LABELS = {
    "id": "ID", "source_key": "구분 key", "name": "이름", "enabled": "수집 켜짐", "list_url": "목록 주소",
    "public_raw_text": "원문 공개", "last_successful_run_at": "마지막 성공", "source_proxy": "수집 proxy",
    "mode": "방식", "status": "상태", "discovered_count": "확인한 게시글", "changed_count": "새·바뀐 공고",
    "failed_count": "실패", "started_at": "시작", "finished_at": "종료", "crawl_run_id": "수집 실행",
    "stage": "단계", "job_key": "작업 key", "attempt_count": "시도", "max_attempts": "최대 시도",
    "available_at": "실행 예정", "error_code": "오류", "error_message": "오류 내용", "error_summary": "오류",
    "current_notice_version_id": "현재 버전", "external_post_id": "게시글 번호", "source": "수집 대상",
    "title": "제목", "availability_status": "원문 상태", "scope_status": "범위", "last_checked_at": "마지막 확인",
    "document": "문서", "notice": "공고", "parser_name": "파서", "parser_version": "파서 버전",
    "quality_flags": "품질 메모", "opportunity_id": "장학", "opportunity": "장학", "version_no": "버전",
    "publication_state": "공개", "data_quality_status": "품질", "edit_kind": "변경 종류",
    "lifecycle_status": "장학 상태", "merged_into_id": "병합 대상", "created_at": "생성",
    "notice_version_id": "공고 버전", "provider_name": "API 종류", "model_id": "모델",
    "prompt_version": "프롬프트", "schema_version": "스키마", "processing_code_version": "코드 버전",
    "input_tokens": "입력 토큰", "output_tokens": "출력 토큰", "estimated_cost": "추정 비용",
    "actor_kind": "주체", "actor_id": "주체 ID", "actor": "수정한 사람", "action": "작업", "entity_type": "대상",
    "entity_id": "대상 ID", "reason": "사유", "email": "이메일", "display_name": "이름", "role": "역할",
    "is_active": "사용 중", "last_login_at": "마지막 로그인", "profile_key": "프로필", "dimensions": "차원",
    "distance_metric": "거리 계산", "chunker_version": "chunker", "field_path": "항목", "scope_key": "범위",
    "value_json": "값",
}
SEOUL_TZ = ZoneInfo("Asia/Seoul")


def _kst(value: Any, seconds: bool = False) -> str:
    """Admin pages show times in Asia/Seoul, to the minute (or second)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(SEOUL_TZ)
        return value.strftime("%Y-%m-%d %H:%M:%S" if seconds else "%Y-%m-%d %H:%M")
    return "" if value is None else str(value)


def _duration(start: Any, end: Any) -> str:
    minutes = int((end - start).total_seconds() // 60) if start and end else 0
    return f"{minutes // 60}시간 {minutes % 60}분" if minutes >= 60 else f"{minutes}분"


def _job_time(job: Any) -> str:
    """What a job's time means depends on its state: queued, waiting to retry, running or done."""
    status = job.get("status")
    started = job.get("locked_at") or job.get("started_at")
    if status == "running" and started:
        return f"{_kst(started)} 시작 (대기열 {_duration(job.get('created_at'), started)})"
    if status == "retry":
        return f"{_kst(job.get('available_at'))} 재시도 예정"
    if status == "queued":
        return f"{_kst(job.get('created_at'))} 등록, 대기 중"
    if job.get("finished_at"):
        return f"{_kst(job.get('finished_at'))} 종료"
    return f"{_kst(job.get('created_at'))} 등록"


templates.env.filters["kst"] = _kst
templates.env.globals["job_time"] = _job_time


def _pretty_json(value: Any, indent: int | None = None) -> str:
    """JSON for display. Unlike ``tojson`` it keeps quotes and Korean as written ("'25.8.25.",
    not "\\u002725.8.25."); Jinja autoescaping still makes it safe inside HTML."""
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


templates.env.filters["pretty_json"] = _pretty_json
templates.env.globals["column_label"] = lambda column: COLUMN_LABELS.get(column, column)
admin_text.register(templates.env)
settings = get_settings()
serializer = URLSafeTimedSerializer(settings.session_secret.get_secret_value(), salt="policy-admin")
passwords = PasswordHasher()
ROLE_LEVEL = {"viewer": 0, "operator": 1, "reviewer": 2, "admin": 3}
LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)
Session = Annotated[AsyncSession, Depends(get_session)]


def _password_stamp(changed_at: Any) -> str:
    """Sessions record when the password last changed; a reset or change ends older sessions."""
    return changed_at.isoformat() if changed_at else ""


def _session_cookie(user_id: uuid.UUID, csrf: str, changed_at: Any = None) -> str:
    return serializer.dumps({"uid": str(user_id), "csrf": csrf, "pw": _password_stamp(changed_at)})


def _set_session(response: Response, user_id: uuid.UUID, changed_at: Any) -> None:
    response.set_cookie("policy_admin", _session_cookie(user_id, secrets.token_urlsafe(24), changed_at),
                        httponly=True, secure=settings.app_env != "local", samesite="strict",
                        max_age=12 * 60 * 60)


MIN_PASSWORD_LENGTH = 12
# While a temporary password is in use, only these pages are reachable.
PASSWORD_CHANGE_PATHS = ("/admin/account", "/admin/logout")


async def current_admin(request: Request, session: Session) -> dict[str, Any]:
    token = request.cookies.get("policy_admin")
    if not token:
        raise HTTPException(303, headers={"Location": "/admin/login"})
    try:
        payload = serializer.loads(token, max_age=12 * 60 * 60)
        user_id = uuid.UUID(payload["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError) as exc:
        raise HTTPException(303, headers={"Location": "/admin/login?expired=1"}) from exc
    row = (await session.execute(text("""
        SELECT id, email, display_name, role, password_change_required, password_changed_at
        FROM inha_policy.admin_users WHERE id=:id AND is_active
    """), {"id": user_id})).mappings().one_or_none()
    if row is None or payload.get("pw", "") != _password_stamp(row["password_changed_at"]):
        raise HTTPException(303, headers={"Location": "/admin/login?expired=1"})
    if row["password_change_required"] and not request.url.path.startswith(PASSWORD_CHANGE_PATHS):
        raise HTTPException(303, headers={"Location": "/admin/account"})
    # Badge counts for the side navigation on every page.
    nav = dict((await session.execute(text("""
        SELECT
          (SELECT count(*) FROM inha_policy.review_items WHERE status IN ('open','in_review')) AS reviews,
          (SELECT count(*) FROM inha_policy.opportunity_versions ov
             JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
            WHERE ov.publication_state='draft' AND o.lifecycle_status <> 'merged'
              AND ov.version_no=(SELECT max(version_no) FROM inha_policy.opportunity_versions x
                                 WHERE x.opportunity_id=ov.opportunity_id)) AS drafts,
          (SELECT count(*) FROM inha_policy.crawl_jobs WHERE status='failed') AS failed_jobs
    """))).mappings().one())
    return {**dict(row), "csrf": payload["csrf"], "nav": nav}


def require_role(role: str) -> Callable:
    async def dependency(admin: Annotated[dict[str, Any], Depends(current_admin)]) -> dict[str, Any]:
        if ROLE_LEVEL[admin["role"]] < ROLE_LEVEL[role]:
            raise HTTPException(403, f"'{admin_text.label('role', role)}' 역할 이상만 쓸 수 있는 기능입니다. "
                                     "필요하면 관리자에게 역할 변경을 요청하세요.")
        return admin
    return dependency


Viewer = Annotated[dict[str, Any], Depends(require_role("viewer"))]
Operator = Annotated[dict[str, Any], Depends(require_role("operator"))]
Reviewer = Annotated[dict[str, Any], Depends(require_role("reviewer"))]
Admin = Annotated[dict[str, Any], Depends(require_role("admin"))]


async def _form(request: Request, admin: dict[str, Any]) -> Any:
    form = await request.form()
    if not secrets.compare_digest(str(form.get("csrf", "")), admin["csrf"]):
        raise HTTPException(403, "보안 확인 값이 맞지 않아 처리하지 않았습니다. 화면을 연 뒤 다시 로그인했거나 "
                                 "화면이 오래되었을 수 있습니다. 화면을 새로고침한 뒤 다시 시도하세요.")
    return form


def _context(request: Request, admin: dict[str, Any], **values: Any) -> dict[str, Any]:
    return {"request": request, "admin": admin, "csrf": admin["csrf"], **values}


def _back_link(request: Request) -> str:
    """The admin page the visitor came from, for "go back" links; never another site."""
    referer = request.headers.get("referer", "")
    parts = urlsplit(referer)
    if parts.netloc == request.url.netloc and parts.path.startswith("/admin") and parts.path != request.url.path:
        return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/admin"


async def _error_page(request: Request, status: int, detail: str) -> HTMLResponse:
    """Errors on admin pages as a page staff can read, with the way back, instead of raw JSON."""
    from .db import SessionFactory

    admin = None
    try:
        async with SessionFactory() as session:
            admin = await current_admin(request, session)
    except Exception:  # not signed in (or the database is the problem): show the page without the menu
        admin = None
    title = ERROR_PAGE_TITLES.get(status, "처리하지 못했습니다")
    if status == 404 and detail in {"", "Not Found"}:
        detail = "주소가 잘못되었거나, 삭제·병합되어 더 이상 없는 항목입니다."
    return templates.TemplateResponse(request, "error.html", {
        "request": request, "admin": admin, "csrf": admin["csrf"] if admin else "",
        "status": status, "title": title, "detail": detail, "back": _back_link(request)},
        status_code=status)


async def admin_http_error(request: Request, exc: Any) -> Response:
    if not request.url.path.startswith("/admin") or exc.status_code < 400 or request.method == "HEAD":
        return await http_exception_handler(request, exc)
    return await _error_page(request, exc.status_code, str(exc.detail or ""))


async def admin_validation_error(request: Request, exc: RequestValidationError) -> Response:
    if not request.url.path.startswith("/admin"):
        return await request_validation_exception_handler(request, exc)
    if exc.errors() and all(error.get("loc", [""])[0] == "path" for error in exc.errors()):
        return await _error_page(request, 404, "")  # a mistyped or truncated link
    fields = ", ".join(str(error.get("loc", ["?"])[-1]) for error in exc.errors()[:5])
    return await _error_page(request, 422, f"입력값이 비었거나 형식이 맞지 않습니다 (항목: {fields}). "
                                           "이전 화면으로 돌아가 다시 입력하세요.")


NO_REASON = "사유 미기재"


def _reason(form: Any) -> str:
    """Reasons are optional; an empty one is recorded as such in the audit log."""
    return str(form.get("reason", "")).strip() or NO_REASON


async def _audit(session: AsyncSession, admin: dict[str, Any], action: str,
                 entity_type: str, entity_id: str, before: Any = None,
                 after: Any = None, reason: str | None = None) -> None:
    await session.execute(text("""
        INSERT INTO inha_policy.audit_logs
          (actor_kind, actor_id, action, entity_type, entity_id, before_state,
           after_state, reason, automated)
        VALUES ('admin_user', :actor, :action, :entity_type, :entity_id,
                CAST(:before AS jsonb), CAST(:after AS jsonb), :reason, false)
    """), {"actor": str(admin["id"]), "action": action, "entity_type": entity_type,
             "entity_id": entity_id, "before": json.dumps(before, default=str),
             "after": json.dumps(after, default=str),
             "reason": reason})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, session: Session, email: Annotated[str, Form()],
                password: Annotated[str, Form()]) -> HTMLResponse:
    address = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = LOGIN_ATTEMPTS[address]
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    if len(attempts) >= 8:
        return templates.TemplateResponse(request, "login.html", {
            "error": "로그인 시도가 너무 많습니다. 5분 뒤에 다시 시도하세요."}, status_code=429)
    attempts.append(now)
    row = (await session.execute(text("""
        SELECT id, password_hash, password_change_required, password_changed_at
        FROM inha_policy.admin_users WHERE email=:email AND is_active
    """), {"email": email.strip().lower()})).mappings().one_or_none()
    try:
        valid = bool(row and row["password_hash"] and passwords.verify(row["password_hash"], password))
    except VerifyMismatchError:
        valid = False
    if not valid:
        return templates.TemplateResponse(request, "login.html", {
            "error": "이메일 또는 비밀번호가 맞지 않습니다. 비밀번호를 잊었으면 관리자에게 초기화를 요청하세요.",
            "email": email}, status_code=401)
    attempts.clear()
    await session.execute(text(
        "UPDATE inha_policy.admin_users SET last_login_at=now() WHERE id=:id"
    ), {"id": row["id"]})
    await session.commit()
    response = RedirectResponse("/admin/account" if row["password_change_required"] else "/admin",
                                status_code=303)
    _set_session(response, row["id"], row["password_changed_at"])
    return response


@router.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, admin: Viewer) -> HTMLResponse:
    return templates.TemplateResponse(request, "account.html", _context(
        request, admin, changed=request.query_params.get("changed"),
        error=request.query_params.get("error")))


@router.post("/account/password")
async def change_password(request: Request, session: Session, admin: Viewer) -> RedirectResponse:
    form = await _form(request, admin)
    current, new = str(form.get("current_password", "")), str(form.get("new_password", ""))
    stored = (await session.execute(text(
        "SELECT password_hash FROM inha_policy.admin_users WHERE id=:id FOR UPDATE"
    ), {"id": admin["id"]})).scalar_one()
    try:
        valid = bool(stored and passwords.verify(stored, current))
    except VerifyMismatchError:
        valid = False
    problem = ("현재 비밀번호가 맞지 않습니다. 비밀번호를 바꾸지 않았습니다." if not valid else
               f"새 비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다." if len(new) < MIN_PASSWORD_LENGTH else
               "새 비밀번호와 확인 칸이 서로 다릅니다. 같은 비밀번호를 두 번 입력하세요." if new != str(form.get("confirm_password", "")) else
               "현재 비밀번호와 다른 새 비밀번호를 정하세요." if new == current else None)
    if problem:
        return RedirectResponse(f"/admin/account?{urlencode({'error': problem})}", status_code=303)
    changed_at = (await session.execute(text("""
        UPDATE inha_policy.admin_users SET password_hash=:hash, password_change_required=false,
          password_changed_at=clock_timestamp(), updated_at=now()
        WHERE id=:id RETURNING password_changed_at
    """), {"id": admin["id"], "hash": passwords.hash(new)})).scalar_one()
    await _audit(session, admin, "admin_password_changed", "admin_user", str(admin["id"]),
                 reason="self-service")
    await session.commit()
    # Other sessions of this account end; this one continues with a fresh cookie.
    response = RedirectResponse("/admin/account?changed=1", status_code=303)
    _set_session(response, admin["id"], changed_at)
    return response


@router.post("/logout")
async def logout(request: Request, admin: Viewer) -> RedirectResponse:
    await _form(request, admin)
    response = RedirectResponse("/admin/login?logged_out=1", status_code=303)
    response.delete_cookie("policy_admin")
    return response


TABLE_QUERIES = {
    "sources": ("수집 대상", "SELECT s.id, s.name, s.source_key, s.enabled, s.list_url, coalesce((s.crawl_config->>'public_raw_text')::boolean, false) AS public_raw_text, s.last_successful_run_at, (SELECT es.masked_value FROM inha_policy.encrypted_secrets es WHERE es.secret_key='crawl.proxy.' || s.source_key AND es.is_active ORDER BY es.version_no DESC LIMIT 1) AS source_proxy FROM inha_policy.sources s ORDER BY s.name"),
    "crawl-runs": ("수집 이력", "SELECT r.id, s.name AS source, r.mode, r.status, r.discovered_count, r.changed_count, r.failed_count, r.started_at, r.finished_at, r.error_summary FROM inha_policy.crawl_runs r LEFT JOIN inha_policy.sources s ON s.id=r.source_id ORDER BY r.scheduled_for DESC LIMIT 200"),
    "notices": ("공고(원시 테이블)", "SELECT n.id, n.current_notice_version_id, n.external_post_id, nv.title, n.availability_status, n.scope_status, n.last_checked_at FROM inha_policy.notices n LEFT JOIN inha_policy.notice_versions nv ON nv.id=n.current_notice_version_id ORDER BY n.last_checked_at DESC NULLS LAST LIMIT 300"),
    "documents": ("파싱 문서", "SELECT d.id, coalesce(nva.original_filename, CASE WHEN nva.role='inline_image' THEN '본문 이미지' ELSE '게시글 본문' END) AS document, nv.title AS notice, n.id AS notice_id, d.parser_name, d.parser_version, d.status, d.quality_flags, d.finished_at FROM inha_policy.documents d JOIN inha_policy.notice_versions nv ON nv.id=d.notice_version_id JOIN inha_policy.notices n ON n.id=nv.notice_id LEFT JOIN inha_policy.notice_version_assets nva ON nva.id=d.asset_occurrence_id ORDER BY d.finished_at DESC NULLS LAST LIMIT 300"),
    "opportunities": ("장학 버전(원시 테이블)", "SELECT ov.id, ov.opportunity_id, ov.version_no, ov.title, ov.publication_state, ov.data_quality_status, ov.edit_kind, o.lifecycle_status, o.merged_into_id, ov.created_at FROM inha_policy.opportunity_versions ov JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id ORDER BY ov.created_at DESC LIMIT 300"),
    "ai-runs": ("AI 추출 실행", "SELECT er.id, nv.title AS notice, nv.notice_id, er.model_id, er.provider_name, er.prompt_version, er.status, er.error_code, left(er.error_message, 1000) AS error_message, er.input_tokens, er.output_tokens, er.estimated_cost, er.finished_at FROM inha_policy.extraction_runs er LEFT JOIN inha_policy.notice_versions nv ON nv.id=er.notice_version_id ORDER BY er.created_at DESC LIMIT 300"),
    "users": ("사용자", "SELECT id, email, display_name, role, is_active, last_login_at, created_at FROM inha_policy.admin_users WHERE deleted_at IS NULL ORDER BY email"),
    "embedding-profiles": ("임베딩 프로필", "SELECT id, profile_key, model_id, dimensions, distance_metric, chunker_version, is_active, created_at FROM inha_policy.embedding_profiles ORDER BY created_at DESC"),
    "manual-overrides": ("수동 수정 이력", "SELECT mo.id, (SELECT ov.title FROM inha_policy.opportunity_versions ov WHERE ov.opportunity_id=mo.opportunity_id ORDER BY ov.version_no DESC LIMIT 1) AS opportunity, mo.opportunity_id, mo.field_path, mo.action, mo.value_json, mo.reason, coalesce(u.deleted_email, u.email, mo.actor_id::text) AS actor, mo.created_at FROM inha_policy.manual_overrides mo LEFT JOIN inha_policy.admin_users u ON u.id::text=mo.actor_id::text ORDER BY mo.created_at DESC LIMIT 300"),
}
# Which label vocabulary (admin_text.LABELS) a column's values use, per table.
TABLE_DOMAINS = {
    "crawl-runs": {"mode": "run_mode", "status": "run_status"},
    "notices": {"availability_status": "availability", "scope_status": "scope"},
    "documents": {"status": "doc_status"},
    "opportunities": {"publication_state": "publication", "data_quality_status": "quality",
                      "edit_kind": "edit_kind", "lifecycle_status": "lifecycle"},
    "ai-runs": {"status": "extraction_status"},
    "users": {"role": "role"},
    "manual-overrides": {"action": "override_action"},
}
# Columns used only for links or tooltips.
TABLE_HIDDEN = {
    "documents": ["notice_id"], "ai-runs": ["notice_id", "provider_name", "error_message"],
    "manual-overrides": ["opportunity_id"],
}
TABLE_LEDES = {
    "sources": "수집기가 확인하는 게시판입니다. '지금 수집'을 누르면 정기 수집을 기다리지 않고 바로 새 글을 확인합니다.",
    "crawl-runs": "수집기가 게시판을 확인한 기록입니다. 지금 수집하려면 수집 대상 화면에서 '지금 수집'을 누르세요.",
    "documents": "게시글 본문과 첨부를 읽어(파싱) 블록으로 나눈 결과입니다. 문서를 누르면 원문과 나란히 봅니다.",
    "ai-runs": "공고마다 AI 모델로 장학 정보를 추출한 기록입니다. 실패한 실행은 공고 화면에서 'AI 재추출'로 다시 요청할 수 있습니다.",
    "embedding-profiles": "검색 색인(임베딩)을 만든 모델별 묶음입니다. 검색은 '사용 중'인 프로필 하나만 씁니다. 모델 변경은 AI 설정 화면에서 하세요.",
    "manual-overrides": "사람이 장학 값을 직접 고친 기록입니다. 고친 값은 공개 API에서 추출된 값보다 우선합니다.",
}


@router.get("/table/{table_key}", response_class=HTMLResponse)
async def table_page(table_key: str, request: Request, session: Session, admin: Viewer) -> HTMLResponse:
    if table_key == "jobs":
        return await jobs_page(request, session, admin)
    if table_key == "audit":
        return await audit_page(request, session, admin)
    entry = TABLE_QUERIES.get(table_key)
    if entry is None:
        raise HTTPException(404)
    title, query = entry
    rows = [dict(row) for row in (await session.execute(text(query))).mappings().all()]
    columns = list(rows[0]) if rows else []
    return templates.TemplateResponse(request, "table.html",
                                      _context(request, admin, title=title, rows=rows, columns=columns,
                                               table_key=table_key, domains=TABLE_DOMAINS.get(table_key, {}),
                                               hidden=TABLE_HIDDEN.get(table_key, []),
                                               lede=TABLE_LEDES.get(table_key)))


JOB_FILTERS = ("failed", "retry", "queued", "running", "succeeded")


async def jobs_page(request: Request, session: AsyncSession, admin: dict[str, Any]) -> HTMLResponse:
    status = request.query_params.get("status", "")
    stage = request.query_params.get("stage", "")
    status = status if status in JOB_FILTERS else ""
    stage = stage if stage in admin_text.LABELS["job_stage"] else ""
    counts = {row["status"]: row["count"] for row in (await session.execute(text("""
        SELECT status, count(*) AS count FROM inha_policy.crawl_jobs
        WHERE (:stage = '' OR stage = :stage) GROUP BY status
    """), {"stage": stage})).mappings()}
    rows = (await session.execute(text("""
        SELECT j.id, j.stage, j.status, j.job_key, j.attempt_count, j.max_attempts, j.available_at,
               j.error_code, left(j.error_message, 2000) AS error_message, j.created_at, j.started_at,
               j.locked_at,
               j.finished_at, j.payload, j.result_metadata, coalesce(nv.notice_id, j.notice_id) AS notice_id,
               nv.title AS notice_title, j.notice_version_id
        FROM inha_policy.crawl_jobs j
        LEFT JOIN inha_policy.notice_versions nv ON nv.id=j.notice_version_id
        WHERE (:status = '' OR j.status = :status) AND (:stage = '' OR j.stage = :stage)
        ORDER BY (j.status = 'running') DESC, j.created_at DESC LIMIT 300
    """), {"status": status, "stage": stage})).mappings().all()
    return templates.TemplateResponse(request, "jobs.html", _context(
        request, admin, rows=rows, counts=counts, status=status, stage=stage,
        total=sum(counts.values())))


async def audit_page(request: Request, session: AsyncSession, admin: dict[str, Any]) -> HTMLResponse:
    who = request.query_params.get("who", "")
    who = who if who in {"people", "auto"} else ""
    entity = request.query_params.get("entity", "").strip()
    rows = (await session.execute(text("""
        WITH a AS (
            SELECT * FROM inha_policy.audit_logs
            WHERE (:who = '' OR (:who = 'people') = (actor_kind = 'admin_user'))
              AND (:entity = '' OR entity_id = :entity)
            ORDER BY created_at DESC LIMIT 300
        )
        SELECT a.created_at, a.actor_kind, a.actor_id, a.action, a.entity_type, a.entity_id, a.reason,
               a.before_state, a.after_state,
               coalesce(u.deleted_email, u.email) AS actor_email, u.display_name AS actor_display,
               t.link, t.subject, t.review_kind
        FROM a
        LEFT JOIN inha_policy.admin_users u
          ON a.actor_kind = 'admin_user' AND u.id::text = a.actor_id
        LEFT JOIN LATERAL (
          SELECT CASE a.entity_type
                   WHEN 'notice' THEN '/admin/notices/' || a.entity_id
                   WHEN 'opportunity' THEN '/admin/opportunities/' || a.entity_id
                   WHEN 'opportunity_version' THEN '/admin/opportunities/' || ov.opportunity_id
                   WHEN 'notice_version' THEN '/admin/notices/' || nv.notice_id
                   WHEN 'document' THEN '/admin/documents/' || a.entity_id
                   WHEN 'review_item' THEN coalesce('/admin/opportunities/' || r.opportunity_id,
                                                    '/admin/notices/' || rnv.notice_id)
                   WHEN 'crawl_run' THEN '/admin/table/crawl-runs'
                   WHEN 'crawl_job' THEN '/admin/table/jobs'
                   WHEN 'admin_user' THEN '/admin/users'
                   WHEN 'api_key' THEN '/admin/api-keys'
                   WHEN 'source' THEN '/admin/table/sources'
                   WHEN 'embedding_profile' THEN '/admin/table/embedding-profiles'
                   WHEN 'runtime_setting' THEN '/admin/settings'
                   WHEN 'encrypted_secret' THEN '/admin/settings'
                   WHEN 'ai_provider' THEN '/admin/settings'
                 END AS link,
                 coalesce(ntitle.title, otitle.title, ov.title, nv.title, au.email, k.name,
                          CASE WHEN a.entity_type IN ('runtime_setting', 'encrypted_secret', 'ai_provider')
                               THEN a.entity_id END) AS subject,
                 r.review_kind
          FROM (SELECT CASE WHEN a.entity_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
                            THEN a.entity_id::uuid END AS uid) ids
          LEFT JOIN inha_policy.review_items r ON a.entity_type = 'review_item' AND r.id = ids.uid
          LEFT JOIN inha_policy.notice_versions rnv ON rnv.id = r.entity_id
          LEFT JOIN inha_policy.opportunity_versions ov ON a.entity_type = 'opportunity_version' AND ov.id = ids.uid
          LEFT JOIN inha_policy.notice_versions nv ON a.entity_type = 'notice_version' AND nv.id = ids.uid
          LEFT JOIN inha_policy.admin_users au ON a.entity_type = 'admin_user' AND au.id = ids.uid
          LEFT JOIN inha_policy.api_keys k ON a.entity_type = 'api_key' AND k.id = ids.uid
          LEFT JOIN LATERAL (SELECT nvx.title FROM inha_policy.notices n
                             JOIN inha_policy.notice_versions nvx ON nvx.id = n.current_notice_version_id
                             WHERE a.entity_type = 'notice' AND n.id = ids.uid) ntitle ON true
          LEFT JOIN LATERAL (SELECT ovx.title FROM inha_policy.opportunity_versions ovx
                             WHERE ovx.opportunity_id = coalesce(CASE WHEN a.entity_type = 'opportunity'
                                                                      THEN ids.uid END, r.opportunity_id)
                             ORDER BY ovx.version_no DESC LIMIT 1) otitle ON true
        ) t ON true
        ORDER BY a.created_at DESC
    """), {"who": who, "entity": entity})).mappings().all()
    return templates.TemplateResponse(request, "audit.html", _context(
        request, admin, rows=rows, who=who, entity=entity))


@router.get("/notices/{notice_id}/diff", response_class=HTMLResponse)
async def notice_diff(notice_id: uuid.UUID, request: Request, session: Session,
                      admin: Viewer) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT revision_no, title, body_text FROM inha_policy.notice_versions
        WHERE notice_id=:id ORDER BY revision_no DESC LIMIT 2
    """), {"id": notice_id})).mappings().all()
    if not rows:
        raise HTTPException(404)
    if len(rows) == 1:
        diff = ["처음 수집한 본문이라 비교할 이전 본문이 없습니다. 아래는 현재 본문입니다.", "", rows[0]["body_text"]]
        labels = ("없음", f"{rows[0]['revision_no']}번째 수집본")
    else:
        newest, previous = rows[0], rows[1]
        labels = (f"{previous['revision_no']}번째 수집본", f"{newest['revision_no']}번째 수집본")
        diff = difflib.unified_diff(previous["body_text"].splitlines(),
                                    newest["body_text"].splitlines(),
                                    fromfile=labels[0], tofile=labels[1], lineterm="")
    return templates.TemplateResponse(request, "diff.html",
                                      _context(request, admin, title=rows[0]["title"],
                                               labels=labels, diff="\n".join(diff),
                                               back=f"/admin/notices/{notice_id}",
                                               lede="게시글 본문이 수정되었을 때 이전 수집본과 비교합니다. "
                                                    "'-'로 시작하는 줄은 지워진 내용, '+'로 시작하는 줄은 새로 생긴 내용입니다."))


@router.get("/opportunities/{opportunity_id}/diff", response_class=HTMLResponse)
async def opportunity_diff(opportunity_id: uuid.UUID, request: Request, session: Session,
                           admin: Viewer) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT version_no, title,
               to_jsonb(ov) - 'id' - 'opportunity_id' - 'supersedes_version_id'
               || jsonb_build_object('application_windows', (
                    SELECT coalesce(jsonb_agg(to_jsonb(w) - 'id' - 'opportunity_version_id'
                                              ORDER BY w.window_key), '[]'::jsonb)
                    FROM inha_policy.application_windows w WHERE w.opportunity_version_id=ov.id))
               AS value
        FROM inha_policy.opportunity_versions ov WHERE opportunity_id=:id
        ORDER BY version_no DESC LIMIT 2
    """), {"id": opportunity_id})).mappings().all()
    if not rows:
        raise HTTPException(404)
    newest = json.dumps(rows[0]["value"], ensure_ascii=False, indent=2, default=str).splitlines()
    if len(rows) == 1:
        labels, diff = ("없음", f"버전 {rows[0]['version_no']}"), newest
    else:
        previous = json.dumps(rows[1]["value"], ensure_ascii=False, indent=2, default=str).splitlines()
        labels = (f"버전 {rows[1]['version_no']}", f"버전 {rows[0]['version_no']}")
        diff = difflib.unified_diff(previous, newest, fromfile=labels[0], tofile=labels[1], lineterm="")
    return templates.TemplateResponse(request, "diff.html",
                                      _context(request, admin, title=rows[0]["title"],
                                               labels=labels, diff="\n".join(diff),
                                               back=f"/admin/opportunities/{opportunity_id}",
                                               lede="장학의 최근 두 버전을 비교합니다. '-'로 시작하는 줄은 이전 버전, "
                                                    "'+'로 시작하는 줄은 새 버전의 값입니다."))


@router.get("/opportunities/{opportunity_id}", response_class=HTMLResponse)
async def opportunity_detail(opportunity_id: uuid.UUID, request: Request, session: Session,
                             admin: Viewer) -> HTMLResponse:
    opportunity = (await session.execute(text("""
        SELECT id, lifecycle_status, merged_into_id, current_version_id, program_key
        FROM inha_policy.opportunities WHERE id=:id
    """), {"id": opportunity_id})).mappings().one_or_none()
    if opportunity is None:
        raise HTTPException(404)
    versions = (await session.execute(text("""
        SELECT id, version_no, edit_kind, publication_state, data_quality_status, created_at
        FROM inha_policy.opportunity_versions WHERE opportunity_id=:id ORDER BY version_no DESC
    """), {"id": opportunity_id})).mappings().all()
    wanted = request.query_params.get("version")
    version_id = next((row["id"] for row in versions if str(row["version_no"]) == wanted), versions[0]["id"])
    version = (await session.execute(text(
        "SELECT * FROM inha_policy.opportunity_versions WHERE id=:id"), {"id": version_id})).mappings().one()
    windows = (await session.execute(text("""
        SELECT * FROM inha_policy.application_windows WHERE opportunity_version_id=:id
        ORDER BY end_date NULLS LAST, window_key
    """), {"id": version_id})).mappings().all()
    benefits = (await session.execute(text("""
        SELECT * FROM inha_policy.benefits WHERE opportunity_version_id=:id ORDER BY benefit_key
    """), {"id": version_id})).mappings().all()
    eligibility = (await session.execute(text("""
        SELECT * FROM inha_policy.eligibility_profiles WHERE opportunity_version_id=:id
    """), {"id": version_id})).mappings().one_or_none()
    sources = (await session.execute(text("""
        SELECT s.relation_kind, s.extraction_item_path, s.extraction_run_id, nv.id AS notice_version_id,
               nv.title, n.id AS notice_id, n.canonical_url, n.external_post_id
        FROM inha_policy.opportunity_version_sources s
        JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
        JOIN inha_policy.notices n ON n.id=nv.notice_id
        WHERE s.opportunity_version_id=:id ORDER BY nv.observed_at
    """), {"id": version_id})).mappings().all()
    documents = (await session.execute(text("""
        SELECT DISTINCT ON (d.notice_version_id, d.asset_occurrence_id) d.id, d.parser_name,
               d.parser_version, d.status, d.quality_flags, d.origin_kind, nva.original_filename, nva.role
        FROM inha_policy.documents d
        LEFT JOIN inha_policy.notice_version_assets nva ON nva.id=d.asset_occurrence_id
        WHERE d.notice_version_id IN (SELECT notice_version_id FROM inha_policy.opportunity_version_sources
                                      WHERE opportunity_version_id=:id) AND d.sealed_at IS NOT NULL
        ORDER BY d.notice_version_id, d.asset_occurrence_id, d.finished_at DESC
    """), {"id": version_id})).mappings().all()
    evidence = (await session.execute(text("""
        SELECT e.field_path, e.candidate_value, e.quote_text, e.candidate_status, e.verification_status,
               e.assertion_kind, e.document_id, e.block_id
        FROM inha_policy.field_evidence e WHERE e.opportunity_version_id=:id ORDER BY e.field_path
    """), {"id": version_id})).mappings().all()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evidence:
        # "/opportunities/0/application_windows/1/end" -> "application_windows/1/end"
        key = row["field_path"].split("/", 3)[-1] if row["field_path"].startswith("/opportunities/") else row["field_path"].lstrip("/")
        grouped[key].append(dict(row))
    extraction_item = None
    if sources:
        run = (await session.execute(text(
            "SELECT parsed_output FROM inha_policy.extraction_runs WHERE id=:id"
        ), {"id": sources[0]["extraction_run_id"]})).scalar_one_or_none()
        path = sources[0]["extraction_item_path"].strip("/").split("/")
        if run and len(path) == 2 and path[0] == "opportunities":
            items = run.get("opportunities") or []
            extraction_item = items[int(path[1])] if int(path[1]) < len(items) else None
            if extraction_item and "selection" in extraction_item:
                # Older runs stored "00명" as unreadable; show the placeholder's meaning.
                extraction_item = {**extraction_item,
                                   "selection": interpret_placeholder_counts(extraction_item["selection"])}
    cited = re.findall(r'"block_id":\s*"([0-9a-f-]{36})"', json.dumps(extraction_item or {}))
    block_documents = {str(row["id"]): str(row["document_id"]) for row in (await session.execute(text("""
        SELECT id, document_id FROM inha_policy.document_blocks WHERE id::text = ANY(:ids)
    """), {"ids": sorted(set(cited))})).mappings()}
    reviews = (await session.execute(text("""
        SELECT id, review_kind, status, payload, created_at, resolution_note FROM inha_policy.review_items
        WHERE opportunity_id=:id OR entity_id=:id
           OR entity_id IN (SELECT id FROM inha_policy.opportunity_versions WHERE opportunity_id=:id)
        ORDER BY (status IN ('open', 'in_review', 'ai_pending')) DESC, created_at DESC
    """), {"id": opportunity_id})).mappings().all()
    # Publishing needs the search index; say so before the button is pressed.
    embedded = (await session.execute(text("""
        SELECT EXISTS (SELECT 1 FROM inha_policy.search_chunks
                       WHERE opportunity_version_id=:id AND embedding_status='succeeded')
    """), {"id": version_id})).scalar_one()
    merged_into = None
    if opportunity["merged_into_id"]:
        merged_into = (await session.execute(text("""
            SELECT opportunity_id AS id, title FROM inha_policy.opportunity_versions
            WHERE opportunity_id=:id ORDER BY version_no DESC LIMIT 1
        """), {"id": opportunity["merged_into_id"]})).mappings().one_or_none()
    return templates.TemplateResponse(request, "opportunity.html", _context(
        request, admin, opportunity=opportunity, versions=versions, version=version, windows=windows,
        benefits=benefits, eligibility=eligibility, sources=sources, documents=documents,
        evidence=dict(grouped), extraction_item=extraction_item, reviews=reviews,
        block_documents=block_documents, embedded=embedded, merged_into=merged_into))


@router.get("/documents/{document_id}", response_class=HTMLResponse)
async def document_view(document_id: uuid.UUID, request: Request, session: Session,
                        admin: Viewer) -> HTMLResponse:
    document = (await session.execute(text("""
        SELECT d.*, nva.original_filename, nva.original_url, nva.binary_asset_id,
               ba.detected_mime, ba.byte_size, ba.sha256, nv.notice_id, nv.title AS notice_title,
               (SELECT df.tool FROM inha_policy.derived_files df WHERE df.binary_asset_id=nva.binary_asset_id
                  AND df.kind='pdf_render' ORDER BY df.created_at DESC LIMIT 1) AS rendered_pdf_tool
        FROM inha_policy.documents d
        JOIN inha_policy.notice_versions nv ON nv.id=d.notice_version_id
        LEFT JOIN inha_policy.notice_version_assets nva ON nva.id=d.asset_occurrence_id
        LEFT JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
        WHERE d.id=:id
    """), {"id": document_id})).mappings().one_or_none()
    if document is None:
        raise HTTPException(404)
    blocks = (await session.execute(text("""
        SELECT id, parent_block_id, block_index, block_kind, text_content, table_data,
               page_number, source_path, bbox, ocr_used, ocr_confidence,
               extraction_metadata
        FROM inha_policy.document_blocks WHERE document_id=:id ORDER BY block_index
    """), {"id": document_id})).mappings().all()
    return templates.TemplateResponse(request, "document.html", _context(
        request, admin, document=dict(document), blocks=[dict(row) for row in blocks],
        highlight=request.query_params.get("block"),
        highlight_page=next((row["page_number"] for row in blocks
                             if str(row["id"]) == request.query_params.get("block")), None)))


@router.get("/assets/{asset_id}/download")
async def asset_download(asset_id: uuid.UUID, session: Session, admin: Viewer) -> Response:
    row = (await session.execute(text("""
        SELECT ba.storage_key, ba.detected_mime,
               coalesce(nullif(nva.original_filename, ''), ba.sha256) AS filename
        FROM inha_policy.binary_assets ba
        LEFT JOIN inha_policy.notice_version_assets nva ON nva.binary_asset_id=ba.id
        WHERE ba.id=:id ORDER BY nva.ordinal NULLS LAST LIMIT 1
    """), {"id": asset_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    payload = build_object_store(settings).get(row["storage_key"])
    filename = str(row["filename"]).replace('"', "").replace("\r", "").replace("\n", "")
    return Response(payload, media_type=row["detected_mime"], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "private, no-store",
    })


@router.get("/notices/{notice_id}/assets-diff", response_class=HTMLResponse)
async def notice_assets_diff(notice_id: uuid.UUID, request: Request, session: Session,
                             admin: Viewer) -> HTMLResponse:
    versions = (await session.execute(text("""
        SELECT id, revision_no, title FROM inha_policy.notice_versions
        WHERE notice_id=:id ORDER BY revision_no DESC LIMIT 2
    """), {"id": notice_id})).mappings().all()
    if not versions:
        raise HTTPException(404)
    version_ids = [row["id"] for row in versions]
    assets = (await session.execute(text("""
        SELECT nva.notice_version_id, nva.occurrence_key, nva.role,
               nva.original_filename, nva.original_url, nva.download_status,
               nva.binary_asset_id, ba.sha256, ba.byte_size, ba.detected_mime
        FROM inha_policy.notice_version_assets nva
        LEFT JOIN inha_policy.binary_assets ba ON ba.id=nva.binary_asset_id
        WHERE nva.notice_version_id = ANY(:ids) ORDER BY nva.ordinal
    """), {"ids": version_ids})).mappings().all()
    by_version: dict[uuid.UUID, dict[str, dict[str, Any]]] = {
        item: {} for item in version_ids
    }
    for row in assets:
        serializable = json.loads(json.dumps(dict(row), default=str))
        by_version[row["notice_version_id"]][row["occurrence_key"]] = serializable
    current = by_version[version_ids[0]]
    previous = by_version[version_ids[1]] if len(version_ids) > 1 else {}
    rows: list[dict[str, Any]] = []
    for key in sorted(set(current) | set(previous)):
        before, after = previous.get(key), current.get(key)
        state = "added" if before is None else "removed" if after is None else (
            "changed" if before.get("sha256") != after.get("sha256") else "unchanged")
        rows.append({"occurrence_key": key, "state": state, "before": before, "after": after})
    labels = (f"{versions[1]['revision_no']}번째 수집본" if len(versions) > 1 else "없음",
              f"{versions[0]['revision_no']}번째 수집본")
    return templates.TemplateResponse(request, "asset_diff.html", _context(
        request, admin, title=versions[0]["title"], labels=labels, rows=rows, notice_id=notice_id))


@router.get("/ai-runs/{run_id}/compare", response_class=HTMLResponse)
async def extraction_diff(run_id: uuid.UUID, request: Request, session: Session,
                          admin: Viewer) -> HTMLResponse:
    current = (await session.execute(text("""
        SELECT id, notice_version_id, provider_name, model_id, prompt_version,
               schema_version, parsed_output, created_at
        FROM inha_policy.extraction_runs WHERE id=:id
    """), {"id": run_id})).mappings().one_or_none()
    if current is None:
        raise HTTPException(404)
    previous = (await session.execute(text("""
        SELECT id, provider_name, model_id, prompt_version, schema_version,
               parsed_output, created_at
        FROM inha_policy.extraction_runs
        WHERE notice_version_id=:version AND id<>:id AND parsed_output IS NOT NULL
          AND created_at < :created
        ORDER BY created_at DESC LIMIT 1
    """), {"version": current["notice_version_id"], "id": run_id,
             "created": current["created_at"]})).mappings().one_or_none()
    after = json.dumps(current["parsed_output"], ensure_ascii=False, indent=2,
                       default=str).splitlines()
    if previous:
        before = json.dumps(previous["parsed_output"], ensure_ascii=False, indent=2,
                            default=str).splitlines()
        left = f"{previous['provider_name']}/{previous['model_id']} {previous['prompt_version']}"
        right = f"{current['provider_name']}/{current['model_id']} {current['prompt_version']}"
        output = difflib.unified_diff(before, after, fromfile=left, tofile=right, lineterm="")
        labels = (left, right)
    else:
        labels, output = ("없음", str(current["id"])), after
    return templates.TemplateResponse(request, "diff.html", _context(
        request, admin, title="AI 추출 결과 비교", labels=labels, diff="\n".join(output),
        lede="같은 공고의 바로 이전 AI 추출 결과와 비교합니다. '-'는 이전 결과에만, '+'는 이번 결과에만 있는 줄입니다.",
        back=(f"/admin/notices/{notice_id}" if (notice_id := (await session.execute(text(
            "SELECT notice_id FROM inha_policy.notice_versions WHERE id=:id"
        ), {"id": current["notice_version_id"]})).scalar_one_or_none()) else None)))


@router.post("/sources/{source_id}/crawl")
async def request_crawl(source_id: uuid.UUID, request: Request, session: Session,
                        admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    run_id = uuid.uuid4()
    try:
        await session.execute(text("""
            INSERT INTO inha_policy.crawl_runs
              (id, source_id, mode, status, scheduled_for)
            VALUES (:id, :source, 'manual', 'queued', now())
        """), {"id": run_id, "source": source_id})
        await _audit(session, admin, "crawl_requested", "crawl_run", str(run_id),
                     after={"source_id": str(source_id)}, reason=str(form.get("reason") or "manual run"))
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return RedirectResponse(with_message("/admin/table/crawl-runs", "crawl_active", problem=True),
                                status_code=303)
    return RedirectResponse(with_message("/admin/table/crawl-runs", "crawl_requested"), status_code=303)


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: uuid.UUID, request: Request, session: Session,
                    admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    before = (await session.execute(text("""
        SELECT status, attempt_count, error_code, error_message, heartbeat_at
        FROM inha_policy.crawl_jobs WHERE id=:id FOR UPDATE
    """), {"id": job_id})).mappings().one_or_none()
    if before is None:
        raise HTTPException(404)
    if (before["status"] == "running" and before["heartbeat_at"] is not None
            and before["heartbeat_at"] > datetime.now(UTC) - timedelta(minutes=15)):
        return RedirectResponse(with_message(_next_path(form, "/admin/table/jobs"), "job_running", problem=True),
                                status_code=303)
    await session.execute(text("""
        UPDATE inha_policy.crawl_jobs SET status='queued', attempt_count=0,
          available_at=now(), worker_id=NULL, locked_at=NULL, heartbeat_at=NULL,
          started_at=NULL, finished_at=NULL, error_code=NULL, error_message=NULL
        WHERE id=:id
    """), {"id": job_id})
    await _audit(session, admin, "job_retried", "crawl_job", str(job_id),
                 before=dict(before), after={"status": "queued", "attempt_count": 0},
                 reason=reason)
    await session.commit()
    return RedirectResponse(with_message(_next_path(form, "/admin/table/jobs"), "job_retried"), status_code=303)


@router.post("/sources/{source_id}/toggle")
async def toggle_source(source_id: uuid.UUID, request: Request, session: Session,
                        admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    before = (await session.execute(text(
        "SELECT enabled FROM inha_policy.sources WHERE id=:id FOR UPDATE"
    ), {"id": source_id})).scalar_one_or_none()
    if before is None:
        raise HTTPException(404)
    await session.execute(text(
        "UPDATE inha_policy.sources SET enabled=NOT enabled WHERE id=:id"
    ), {"id": source_id})
    await _audit(session, admin, "source_toggled", "source", str(source_id),
                 before={"enabled": before}, after={"enabled": not before}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message("/admin/table/sources", "source_toggled"), status_code=303)


@router.post("/sources/{source_id}/raw-policy")
async def source_raw_policy(source_id: uuid.UUID, request: Request, session: Session,
                            admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    expose = str(form.get("public_raw_text", "false")).lower() == "true"
    reason = _reason(form)
    found = (await session.execute(text(
        "SELECT crawl_config->'public_raw_text' AS value FROM inha_policy.sources WHERE id=:id FOR UPDATE"
    ), {"id": source_id})).mappings().one_or_none()
    if found is None:
        raise HTTPException(404)
    before = found["value"]
    await session.execute(text("""
        UPDATE inha_policy.sources
        SET crawl_config=jsonb_set(crawl_config, '{public_raw_text}', to_jsonb(CAST(:expose AS boolean)), true)
        WHERE id=:id
    """), {"expose": expose, "id": source_id})
    await _audit(session, admin, "source_raw_policy_updated", "source", str(source_id),
                 before={"public_raw_text": before}, after={"public_raw_text": expose}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message("/admin/table/sources", "source_raw_policy"), status_code=303)


@router.post("/documents/{document_id}/reprocess")
async def reprocess_document(document_id: uuid.UUID, request: Request, session: Session,
                             admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    row = (await session.execute(text("""
        SELECT d.notice_version_id, d.asset_occurrence_id, d.origin_kind,
               nv.notice_id, nv.crawl_run_id
        FROM inha_policy.documents d JOIN inha_policy.notice_versions nv ON nv.id=d.notice_version_id
        WHERE d.id=:id
    """), {"id": document_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    payload = {"origin": row["origin_kind"]}
    if row["asset_occurrence_id"]:
        payload["asset_occurrence_id"] = str(row["asset_occurrence_id"])
    job_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (id, crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:id, :run, 'parse_document', :key, :notice, :version, CAST(:payload AS jsonb))
    """), {"id": job_id, "run": row["crawl_run_id"], "key": f"reparse:{document_id}:{job_id}",
             "notice": row["notice_id"], "version": row["notice_version_id"],
             "payload": json.dumps(payload)})
    await _audit(session, admin, "document_reprocess_queued", "document", str(document_id),
                 after={"job_id": str(job_id)}, reason=str(form.get("reason") or "manual reprocess"))
    await session.commit()
    back = _next_path(form, f"/admin/notices/{row['notice_id']}")
    return RedirectResponse(with_message(back, "reparse_queued"), status_code=303)


@router.post("/notice-versions/{version_id}/extract")
async def reextract(version_id: uuid.UUID, request: Request, session: Session,
                    admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    row = (await session.execute(text("""
        SELECT notice_id, crawl_run_id FROM inha_policy.notice_versions WHERE id=:id
    """), {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    job_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (id, crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:id, :run, 'structure', :key, :notice, :version, '{}'::jsonb)
    """), {"id": job_id, "run": row["crawl_run_id"], "key": f"reextract:{version_id}:{job_id}",
             "notice": row["notice_id"], "version": version_id})
    await _audit(session, admin, "extraction_reprocess_queued", "notice_version", str(version_id),
                 after={"job_id": str(job_id)}, reason=str(form.get("reason") or "manual reprocess"))
    await session.commit()
    back = _next_path(form, f"/admin/notices/{row['notice_id']}")
    return RedirectResponse(with_message(back, "extract_queued"), status_code=303)


@router.post("/opportunity-versions/{version_id}/embed")
async def reembed(version_id: uuid.UUID, request: Request, session: Session,
                  admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    row = (await session.execute(text("""
        SELECT nv.notice_id, nv.crawl_run_id, nv.id AS notice_version_id, ov.opportunity_id
        FROM inha_policy.opportunity_version_sources ovs
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
        WHERE ovs.opportunity_version_id=:id LIMIT 1
    """), {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "이 장학 버전에 연결된 공고가 없어 검색 색인을 만들 수 없습니다.")
    job_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (id, crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:id, :run, 'embed', :key, :notice, :notice_version, CAST(:payload AS jsonb))
    """), {"id": job_id, "run": row["crawl_run_id"], "key": f"reembed:{version_id}:{job_id}",
             "notice": row["notice_id"], "notice_version": row["notice_version_id"],
             "payload": json.dumps({"opportunity_version_id": str(version_id)})})
    await _audit(session, admin, "embedding_reprocess_queued", "opportunity_version", str(version_id),
                 after={"job_id": str(job_id)}, reason=str(form.get("reason") or "manual reprocess"))
    await session.commit()
    return RedirectResponse(with_message(f"/admin/opportunities/{row['opportunity_id']}", "embed_queued"),
                            status_code=303)


@router.post("/embedding-profiles/{profile_id}/backfill")
async def backfill_embedding_profile(profile_id: uuid.UUID, request: Request, session: Session,
                                     admin: Operator) -> RedirectResponse:
    form = await _form(request, admin)
    profile = (await session.execute(text("""
        SELECT model_id, dimensions, config FROM inha_policy.embedding_profiles WHERE id=:id
    """), {"id": profile_id})).mappings().one_or_none()
    if profile is None:
        raise HTTPException(404)
    effective = await effective_configuration(session)
    if (profile["model_id"] != effective["embedding.model"]["value"]
            or profile["dimensions"] != int(effective["embedding.dimensions"]["value"])
            or profile["config"].get("provider") != effective["embedding.provider"]["value"]):
        return RedirectResponse(with_message("/admin/table/embedding-profiles", "profile_mismatch", problem=True),
                                status_code=303)
    rows = (await session.execute(text("""
        SELECT DISTINCT ON (ov.id) ov.id AS version_id, nv.notice_id,
               nv.id AS notice_version_id, nv.crawl_run_id
        FROM inha_policy.opportunity_versions ov
        JOIN inha_policy.opportunity_version_sources ovs ON ovs.opportunity_version_id=ov.id
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        WHERE ov.publication_state IN ('draft','published')
        ORDER BY ov.id, nv.observed_at DESC
    """))).mappings().all()
    batch = uuid.uuid4()
    for row in rows:
        await session.execute(text("""
            INSERT INTO inha_policy.crawl_jobs
              (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
            VALUES (:run, 'embed', :key, :notice, :notice_version, CAST(:payload AS jsonb))
            ON CONFLICT DO NOTHING
        """), {"run": row["crawl_run_id"], "key": f"backfill:{profile_id}:{batch}:{row['version_id']}",
                 "notice": row["notice_id"], "notice_version": row["notice_version_id"],
                 "payload": json.dumps({"opportunity_version_id": str(row["version_id"]), "publish": False})})
    await _audit(session, admin, "embedding_backfill_queued", "embedding_profile", str(profile_id),
                 after={"batch_id": str(batch), "count": len(rows)},
                 reason=str(form.get("reason") or "profile backfill"))
    await session.commit()
    return RedirectResponse(with_message("/admin/table/embedding-profiles", "backfill_queued", n=len(rows)),
                            status_code=303)


@router.post("/embedding-profiles/{profile_id}/activate")
async def activate_embedding_profile(profile_id: uuid.UUID, request: Request, session: Session,
                                     admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    missing = (await session.execute(text("""
        SELECT count(*) FROM inha_policy.current_opportunity_versions ov
        WHERE NOT EXISTS (
          SELECT 1 FROM inha_policy.search_chunks sc
          WHERE sc.opportunity_version_id=ov.id AND sc.embedding_profile_id=:profile
            AND sc.embedding_status='succeeded')
    """), {"profile": profile_id})).scalar_one()
    if missing:
        return RedirectResponse(with_message(_next_path(form, "/admin/table/embedding-profiles"),
                                             "profile_incomplete", problem=True, n=missing), status_code=303)
    await session.execute(text("SELECT pg_advisory_xact_lock(7410932176502::bigint)"))
    before = (await session.execute(text(
        "SELECT profile_key FROM inha_policy.embedding_profiles WHERE is_active"
    ))).scalar_one_or_none()
    await session.execute(text(
        "UPDATE inha_policy.embedding_profiles SET is_active=false WHERE is_active"
    ))
    updated = (await session.execute(text("""
        UPDATE inha_policy.embedding_profiles SET is_active=true WHERE id=:id RETURNING profile_key
    """), {"id": profile_id})).scalar_one_or_none()
    if updated is None:
        raise HTTPException(404)
    await _audit(session, admin, "embedding_profile_activated", "embedding_profile", str(profile_id),
                 before={"profile_key": before}, after={"profile_key": updated},
                 reason=str(form.get("reason") or "profile activation"))
    await session.commit()
    return RedirectResponse(with_message(_next_path(form, "/admin/table/embedding-profiles"), "profile_activated"),
                            status_code=303)


def _db_message(exc: DBAPIError) -> str:
    return str(getattr(exc, "orig", exc)).splitlines()[0][:300]


def _next_path(form: Any, default: str) -> str:
    """Where a form asks to return to; only admin paths, so it cannot redirect off-site."""
    target = str(form.get("next") or "")
    return target if re.fullmatch(r"/admin(?:[/?#][\w\-./#?=&%]*)?", target) and "//" not in target else default


async def _queue_embeddings(session: AsyncSession, admin: dict[str, Any], reason: str) -> int:
    """Queue embedding jobs (index only, publish=false) under the current settings for every latest
    version without a vector in that profile. A new model means a new profile, so all of them."""
    profile = await ensure_profile(session, await effective_configuration(session))
    rows = (await session.execute(text("""
        WITH latest AS (
          SELECT DISTINCT ON (opportunity_id) id FROM inha_policy.opportunity_versions
          ORDER BY opportunity_id, version_no DESC)
        SELECT DISTINCT ON (latest.id) latest.id AS version_id, nv.notice_id,
               nv.id AS notice_version_id, nv.crawl_run_id
        FROM latest
        JOIN inha_policy.opportunity_version_sources ovs ON ovs.opportunity_version_id=latest.id
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        WHERE NOT EXISTS (SELECT 1 FROM inha_policy.search_chunks sc
                          WHERE sc.opportunity_version_id=latest.id AND sc.embedding_profile_id=:profile
                            AND sc.embedding_status='succeeded')
        ORDER BY latest.id, nv.observed_at DESC
    """), {"profile": profile.id})).mappings().all()
    batch = uuid.uuid4()
    for row in rows:
        await session.execute(text("""
            INSERT INTO inha_policy.crawl_jobs
              (crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
            VALUES (:run, 'embed', :key, :notice, :notice_version, CAST(:payload AS jsonb))
            ON CONFLICT DO NOTHING
        """), {"run": row["crawl_run_id"], "key": f"recompute:{profile.id}:{batch}:{row['version_id']}",
                 "notice": row["notice_id"], "notice_version": row["notice_version_id"],
                 "payload": json.dumps({"opportunity_version_id": str(row["version_id"]), "publish": False})})
    await _audit(session, admin, "embedding_recompute_queued", "embedding_profile", str(profile.id),
                 after={"profile_key": profile.key, "batch_id": str(batch), "count": len(rows)},
                 reason=reason)
    return len(rows)


@router.post("/embeddings/recompute")
async def recompute_embeddings(request: Request, session: Session, admin: Operator) -> RedirectResponse:
    """Queue embeddings, under the current settings, for every latest version that lacks one.

    The jobs only index (publish=false). Search keeps using the active profile until an admin
    switches to the new one once it is complete.
    """
    form = await _form(request, admin)
    count = await _queue_embeddings(session, admin, _reason(form))
    await session.commit()
    return RedirectResponse(with_message("/admin/settings", "embeddings_queued", n=count), status_code=303)


@router.get("/reviews", response_class=HTMLResponse)
async def reviews(request: Request, session: Session, admin: Viewer) -> HTMLResponse:
    kind = request.query_params.get("kind", "")
    counts = (await session.execute(text("""
        SELECT review_kind, count(*) AS count FROM inha_policy.review_items
        WHERE status IN ('open','in_review') GROUP BY review_kind ORDER BY count DESC
    """))).mappings().all()
    rows = (await session.execute(text("""
        SELECT r.id, r.review_kind, r.status, r.priority, r.entity_type, r.entity_id,
               coalesce(r.opportunity_id, ovv.opportunity_id) AS opportunity_id, r.field_path, r.payload,
               r.created_at, coalesce(ov.title, ovv.title, nv.title, nv3.title) AS subject_title,
               coalesce(nv.notice_id, nv2.notice_id, nv3.notice_id) AS notice_id,
               nv.id AS notice_version_id, j.error_code AS job_error_code, j.stage AS job_stage
        FROM inha_policy.review_items r
        LEFT JOIN LATERAL (SELECT title FROM inha_policy.opportunity_versions
                           WHERE opportunity_id=r.opportunity_id ORDER BY version_no DESC LIMIT 1) ov ON true
        LEFT JOIN inha_policy.opportunity_versions ovv
          ON r.entity_type='opportunity_version' AND ovv.id=r.entity_id
        LEFT JOIN inha_policy.crawl_jobs j
          ON j.id = CASE WHEN r.payload->>'job_id' ~ '^[0-9a-f-]{36}$' THEN (r.payload->>'job_id')::uuid END
        LEFT JOIN inha_policy.notice_versions nv ON nv.id=r.entity_id
        LEFT JOIN LATERAL (SELECT nvx.notice_id FROM inha_policy.opportunity_version_sources s
                           JOIN inha_policy.notice_versions nvx ON nvx.id=s.notice_version_id
                           WHERE s.opportunity_version_id=r.entity_id LIMIT 1) nv2 ON true
        LEFT JOIN LATERAL (SELECT nvn.notice_id, nvn.title FROM inha_policy.notices nn
                           JOIN inha_policy.notice_versions nvn ON nvn.id=nn.current_notice_version_id
                           WHERE nn.id=r.entity_id) nv3 ON true
        WHERE r.status IN ('open','in_review') AND (:kind = '' OR r.review_kind = :kind)
        ORDER BY r.priority DESC, r.created_at LIMIT 300
    """), {"kind": kind})).mappings().all()
    ai_pending = (await session.execute(text(
        "SELECT count(*) FROM inha_policy.review_items WHERE status='ai_pending'"
    ))).scalar_one()
    from .admin_pages import REVIEW_LABELS
    return templates.TemplateResponse(request, "reviews.html", _context(
        request, admin, rows=rows, counts=counts, kind=kind, labels=REVIEW_LABELS, ai_pending=ai_pending))


@router.post("/reviews/{review_id}/resolve")
async def resolve_review(review_id: uuid.UUID, request: Request, session: Session,
                         admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    back = _next_path(form, "/admin/reviews")
    before = (await session.execute(text(
        "SELECT status, resolution_note FROM inha_policy.review_items WHERE id=:id FOR UPDATE"
    ), {"id": review_id})).mappings().one_or_none()
    if before is None:
        raise HTTPException(404, "검토 항목을 찾을 수 없습니다.")
    if before["status"] not in {"open", "in_review"}:  # a double submit, or someone else was faster
        return RedirectResponse(with_message(back, "review_closed", problem=True), status_code=303)
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status='resolved', resolution_note=:reason,
          resolved_at=now(), updated_at=now() WHERE id=:id
    """), {"reason": reason, "id": review_id})
    await _audit(session, admin, "review_resolved", "review_item", str(review_id),
                 before=dict(before), after={"status": "resolved"}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message(back, "review_resolved"), status_code=303)


def _override_value(form: Any) -> Any:
    """The new value of a manual edit. Staff type plain text ("새 제목"); a quoted string, an
    object or a list is read as JSON, as before, so ``"2026"`` stays text and ``2026`` does too."""
    raw = str(form.get("value_json", "")).strip()
    if not raw:
        raise HTTPException(422, "새 값을 입력하세요.")
    if raw[0] in '"[{':
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


@router.post("/reviews/{review_id}/apply-override")
async def apply_review_override(review_id: uuid.UUID, request: Request, session: Session,
                                admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    field_path = str(form.get("field_path", "")).strip()
    if not field_path.startswith("/"):
        raise HTTPException(422, "고칠 항목(field_path)을 고르세요.")
    value = _override_value(form)
    review = (await session.execute(text("""
        SELECT id, status, entity_type, entity_id, opportunity_id, payload
        FROM inha_policy.review_items WHERE id=:id FOR UPDATE
    """), {"id": review_id})).mappings().one_or_none()
    if review is None or review["status"] not in {"open", "in_review"}:
        raise HTTPException(409, "이미 처리되었거나 없는 검토 항목입니다.")
    opportunity_id = review["opportunity_id"]
    if opportunity_id is None and review["entity_type"] == "opportunity_version":
        opportunity_id = (await session.execute(text(
            "SELECT opportunity_id FROM inha_policy.opportunity_versions WHERE id=:id"
        ), {"id": review["entity_id"]})).scalar_one_or_none()
    if opportunity_id is None and review["entity_type"] == "notice_version":
        matches = (await session.execute(text("""
            SELECT DISTINCT ov.opportunity_id
            FROM inha_policy.opportunity_version_sources ovs
            JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
            WHERE ovs.notice_version_id=:id
        """), {"id": review["entity_id"]})).scalars().all()
        if len(matches) == 1:
            opportunity_id = matches[0]
    if opportunity_id is None:
        raise HTTPException(409, "이 검토 항목에 연결된 장학이 하나로 정해지지 않아 값을 고칠 수 없습니다. "
                                 "장학 상세 화면에서 직접 수정하세요.")
    override_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.manual_overrides
          (id, opportunity_id, field_path, scope_key, action, value_json, actor_id, reason)
        VALUES (:id, :opportunity, :path, :scope, 'set', CAST(:value AS jsonb), :actor, :reason)
    """), {"id": override_id, "opportunity": opportunity_id, "path": field_path,
             "scope": str(form.get("scope_key") or "main"), "value": json.dumps(value),
             "actor": admin["id"], "reason": reason})
    await session.execute(text("""
        UPDATE inha_policy.review_items SET status='resolved', resolution_note=:reason,
          resolved_at=now(), updated_at=now() WHERE id=:id
    """), {"id": review_id, "reason": reason})
    await session.execute(text("""
        INSERT INTO inha_policy.change_events
          (event_kind, opportunity_id, opportunity_version_id, previous_version_id,
           edit_kind, visible_after, observed_at, payload)
        SELECT 'updated', id, current_version_id, current_version_id, 'other', true, now(),
               jsonb_build_object('manual_override_id', CAST(:override AS text),
                                  'review_id', CAST(:review AS text),
                                  'field_path', CAST(:path AS text))
        FROM inha_policy.opportunities
        WHERE id=:opportunity AND current_version_id IS NOT NULL
    """), {"override": override_id, "review": review_id, "path": field_path,
             "opportunity": opportunity_id})
    await _audit(session, admin, "review_override_applied", "review_item", str(review_id),
                 before={"status": review["status"], "payload": review["payload"]},
                 after={"status": "resolved", "override_id": str(override_id),
                        "opportunity_id": str(opportunity_id), "field_path": field_path,
                        "value": value}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message(_next_path(form, "/admin/reviews"), "review_override_applied"),
                            status_code=303)


@router.get("/reviews/{review_id}/revision", response_class=HTMLResponse)
async def revision_review(review_id: uuid.UUID, request: Request, session: Session, admin: Reviewer,
                          opportunity_id: uuid.UUID | None = None) -> HTMLResponse:
    review = (await session.execute(text("""
        SELECT r.id, r.status, r.entity_id, r.payload, nv.title, nv.notice_id, nv.observed_at
        FROM inha_policy.review_items r
        JOIN inha_policy.notice_versions nv ON nv.id=r.entity_id
        WHERE r.id=:id AND r.review_kind='revision_candidate'
    """), {"id": review_id})).mappings().one_or_none()
    if review is None:
        raise HTTPException(404)
    payload = review["payload"]
    run_id = payload.get("extraction_run_id") or (await session.execute(text("""
        SELECT id FROM inha_policy.extraction_runs
        WHERE notice_version_id=:id AND status='succeeded' AND sealed_at IS NOT NULL
        ORDER BY finished_at DESC LIMIT 1
    """), {"id": review["entity_id"]})).scalar_one_or_none()
    # Candidates are suggestions only; similarity never links anything by itself.
    candidates = (await session.execute(text("""
        SELECT DISTINCT ON (o.id) o.id, ov.title, ov.version_no, ov.academic_year,
               similarity(ov.title, :title) AS score
        FROM inha_policy.opportunities o
        JOIN inha_policy.opportunity_versions ov ON ov.opportunity_id=o.id
        WHERE o.lifecycle_status <> 'merged'
          AND NOT EXISTS (SELECT 1 FROM inha_policy.opportunity_version_sources s
                          JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
                          WHERE s.opportunity_version_id=ov.id AND nv.notice_id=:notice)
        ORDER BY o.id, ov.version_no DESC
    """), {"title": review["title"], "notice": review["notice_id"]})).mappings().all()
    candidates = sorted(candidates, key=lambda row: -(row["score"] or 0))[:30]
    evidence_blocks = {}
    for item in [*payload.get("marker_evidence", []), *payload.get("same_cycle_evidence", []),
                 *(ref for patch in payload.get("patches", []) for ref in patch.get("evidence", []))]:
        evidence_blocks[item["block_id"]] = None
    if evidence_blocks:
        rows = (await session.execute(text("""
            SELECT id::text AS id, text_content FROM inha_policy.document_blocks
            WHERE id::text = ANY(:ids)
        """), {"ids": list(evidence_blocks)})).mappings().all()
        evidence_blocks.update({row["id"]: row["text_content"] for row in rows})
    before = None
    if opportunity_id:
        version = (await session.execute(text("""
            SELECT id, version_no, title, summary, source_status_override
            FROM inha_policy.opportunity_versions WHERE opportunity_id=:id
            ORDER BY version_no DESC LIMIT 1
        """), {"id": opportunity_id})).mappings().one_or_none()
        if version is None:
            raise HTTPException(404, "고른 장학에 버전이 없습니다. 다른 장학을 고르세요.")
        windows = (await session.execute(text("""
            SELECT window_key, window_kind, phase_label, start_date, start_time, end_date, end_time,
                   raw_text FROM inha_policy.application_windows
            WHERE opportunity_version_id=:id ORDER BY window_key
        """), {"id": version["id"]})).mappings().all()
        evidence = (await session.execute(text("""
            SELECT e.id, e.field_path, e.candidate_status, e.candidate_value, e.quote_text,
                   nv.title AS notice_title
            FROM inha_policy.field_evidence e
            JOIN inha_policy.notice_versions nv ON nv.id=e.notice_version_id
            WHERE e.opportunity_version_id=:id
              AND (e.field_path ~ '/(name|start|end)$' OR e.field_path IN ('/title','/summary'))
            ORDER BY e.field_path
        """), {"id": version["id"]})).mappings().all()
        before = {"version": version, "windows": windows, "evidence": evidence}
    return templates.TemplateResponse(request, "revision.html", _context(
        request, admin, review=review, payload=payload, run_id=run_id, candidates=candidates,
        opportunity_id=opportunity_id, before=before, evidence_blocks=evidence_blocks))


@router.post("/opportunities/{opportunity_id}/revisions")
async def apply_revision(opportunity_id: uuid.UUID, request: Request, session: Session,
                         admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    try:
        patches = tuple(
            FieldPatch(field_path=str(item["field_path"]), value=item["value"],
                       evidence=EvidenceInput(uuid.UUID(str(item["block_id"])), str(item["quote"])),
                       supersedes_evidence_ids=(tuple(uuid.UUID(str(value)) for value in
                                                      item["supersedes_evidence_ids"])
                                                if item.get("supersedes_evidence_ids") is not None
                                                else None),
                       scope_key=str(item.get("scope_key") or "main"))
            for item in json.loads(str(form.get("patches_json", "[]"))))
        review_id = uuid.UUID(str(form["review_id"])) if form.get("review_id") else None
        revision = VerifiedRevision(
            opportunity_id=opportunity_id,
            amendment_notice_version_id=uuid.UUID(str(form.get("notice_version_id", ""))),
            extraction_run_id=uuid.UUID(str(form.get("extraction_run_id", ""))),
            extraction_item_path=str(form.get("extraction_item_path") or "/revisions/0"),
            kind=str(form.get("kind", "")),
            intent=EvidenceInput(uuid.UUID(str(form.get("intent_block_id", ""))),
                                 str(form.get("intent_quote", ""))),
            patches=patches,
            same_cycle_verified=form.get("same_cycle_verified") == "on",
            same_scope_verified=form.get("same_scope_verified") == "on",
            new_value_verified=form.get("new_value_verified") == "on",
            reason=_reason(form),
            actor_id=str(admin["id"]),
            review_id=review_id,
        )
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(422, "정정·연장 입력값의 형식이 맞지 않습니다. 블록 ID와 바뀐 값(JSON)을 확인하세요. "
                                 f"({exc.__class__.__name__}: {exc})") from exc
    try:
        await RevisionService(session).apply(revision)
    except RevisionError as exc:
        await session.rollback()
        raise HTTPException(exc.status, f"정정·연장을 적용하지 못했습니다: {exc}") from exc
    await session.commit()
    version_no = (await session.execute(text(
        "SELECT max(version_no) FROM inha_policy.opportunity_versions WHERE opportunity_id=:id"
    ), {"id": opportunity_id})).scalar_one()
    return RedirectResponse(with_message(f"/admin/opportunities/{opportunity_id}?version={version_no}",
                                         "revision_applied"), status_code=303)


async def _opportunity_card(session: AsyncSession, opportunity_id: uuid.UUID) -> dict[str, Any]:
    version = (await session.execute(text("""
        SELECT o.id AS opportunity_id, o.lifecycle_status, o.current_version_id, ov.id, ov.version_no,
               ov.title, ov.provider_name, ov.academic_year, ov.academic_term, ov.round_label,
               ov.summary, ov.data_quality_status
        FROM inha_policy.opportunities o
        JOIN inha_policy.opportunity_versions ov ON ov.opportunity_id=o.id
        WHERE o.id=:id ORDER BY ov.version_no DESC LIMIT 1
    """), {"id": opportunity_id})).mappings().one_or_none()
    if version is None:
        raise HTTPException(404, "장학을 찾을 수 없습니다. 병합되었거나 삭제되었을 수 있습니다.")
    windows = (await session.execute(text("""
        SELECT window_key, window_kind, start_date, start_time, end_date, end_time
        FROM inha_policy.application_windows WHERE opportunity_version_id=:id ORDER BY window_key
    """), {"id": version["id"]})).mappings().all()
    benefits = (await session.execute(text("""
        SELECT benefit_kind, amount_min, amount_max, currency, raw_text
        FROM inha_policy.benefits WHERE opportunity_version_id=:id ORDER BY benefit_key
    """), {"id": version["id"]})).mappings().all()
    sources = (await session.execute(text("""
        SELECT nv.title, n.canonical_url, s.relation_kind, n.id AS notice_id
        FROM inha_policy.opportunity_version_sources s
        JOIN inha_policy.notice_versions nv ON nv.id=s.notice_version_id
        JOIN inha_policy.notices n ON n.id=nv.notice_id
        WHERE s.opportunity_version_id=:id ORDER BY nv.observed_at
    """), {"id": version["id"]})).mappings().all()
    return {"version": version, "windows": windows, "benefits": benefits, "sources": sources}


@router.get("/reviews/{review_id}/identity", response_class=HTMLResponse)
async def identity_review(review_id: uuid.UUID, request: Request, session: Session,
                          admin: Reviewer) -> HTMLResponse:
    review = (await session.execute(text("""
        SELECT id, status, opportunity_id, payload, resolution_note FROM inha_policy.review_items
        WHERE id=:id AND review_kind='identity_uncertain'
    """), {"id": review_id})).mappings().one_or_none()
    if review is None or review["opportunity_id"] is None:
        raise HTTPException(404)
    payload = review["payload"]
    candidate_ids = [item["opportunity_id"] for item in payload.get("candidates", [])]
    candidate_ids += [item for item in payload.get("existing_opportunity_ids", [])
                      if item not in candidate_ids]
    decided = {row["supersedes_decision_id"]: row["decision_status"] for row in (await session.execute(text("""
        SELECT supersedes_decision_id, decision_status FROM inha_policy.identity_decisions
        WHERE supersedes_decision_id = ANY(:ids)
    """), {"ids": [uuid.UUID(item["decision_id"]) for item in payload.get("candidates", [])]})).mappings()}
    candidates = []
    for opportunity_id in candidate_ids:
        meta = next((item for item in payload.get("candidates", [])
                     if item["opportunity_id"] == opportunity_id), {})
        proposal = uuid.UUID(meta["decision_id"]) if meta.get("decision_id") else None
        candidates.append({"card": await _opportunity_card(session, uuid.UUID(opportunity_id)),
                           "meta": meta, "proposal_id": proposal,
                           "decided": decided.get(proposal) if proposal else None})
    return templates.TemplateResponse(request, "identity.html", _context(
        request, admin, review=review, subject=await _opportunity_card(session, review["opportunity_id"]),
        candidates=candidates))


@router.post("/identity-decisions/{decision_id}/reject")
async def reject_identity_proposal(decision_id: uuid.UUID, request: Request, session: Session,
                                   admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    proposal = (await session.execute(text("""
        SELECT decision_kind, opportunity_id, other_opportunity_id, decision_status
        FROM inha_policy.identity_decisions WHERE id=:id
    """), {"id": decision_id})).mappings().one_or_none()
    if proposal is None:
        raise HTTPException(422, "병합 제안을 찾을 수 없습니다.")
    if proposal["decision_status"] != "proposed":
        raise HTTPException(409, "이미 확정된 판정이라 바꿀 수 없습니다.")
    if (await session.execute(text(
        "SELECT EXISTS (SELECT 1 FROM inha_policy.identity_decisions WHERE supersedes_decision_id=:id)"
    ), {"id": decision_id})).scalar_one():
        raise HTTPException(409, "이 후보는 이미 판정했습니다(병합했거나 다른 장학으로 판정함).")
    rejection_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.identity_decisions
          (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
           supersedes_decision_id, identity_basis, rule_version, decision_reason, actor_kind,
           actor_id, observed_at)
        VALUES (:id, :kind, 'rejected', :opportunity, :other, :proposal, 'human_verified',
                'identity-admin-1.0', :reason, 'human', :actor, clock_timestamp())
    """), {"id": rejection_id, "kind": proposal["decision_kind"],
             "opportunity": proposal["opportunity_id"], "other": proposal["other_opportunity_id"],
             "proposal": decision_id, "reason": reason, "actor": str(admin["id"])})
    await _audit(session, admin, "identity_proposal_rejected", "identity_decision", str(decision_id),
                 after={"rejection_id": str(rejection_id)}, reason=reason)
    await session.commit()
    back = _next_path({"next": form.get("return_to") or form.get("next")}, "/admin/reviews")
    return RedirectResponse(with_message(back, "proposal_rejected"), status_code=303)


@router.post("/opportunity-versions/{version_id}/publish")
async def publish(version_id: uuid.UUID, request: Request, session: Session,
                  admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    row = (await session.execute(text("""
        SELECT opportunity_id, publication_state FROM inha_policy.opportunity_versions
        WHERE id=:id FOR UPDATE
    """), {"id": version_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404)
    lifecycle = (await session.execute(text(
        "SELECT lifecycle_status FROM inha_policy.opportunities WHERE id=:id"
    ), {"id": row["opportunity_id"]})).scalar_one()
    if lifecycle == "merged":  # publishing used to undo the merge silently
        raise HTTPException(409, "병합된 장학은 공개할 수 없습니다. 대표 장학에서 공개하거나, 먼저 장학 화면에서 병합을 취소하세요.")
    embedded = (await session.execute(text("""
        SELECT EXISTS (SELECT 1 FROM inha_policy.search_chunks
                       WHERE opportunity_version_id=:id AND embedding_status='succeeded')
    """), {"id": version_id})).scalar_one()
    if not embedded:
        return RedirectResponse(with_message(f"/admin/opportunities/{row['opportunity_id']}", "not_embedded",
                                             problem=True), status_code=303)
    await session.execute(text("""
        UPDATE inha_policy.opportunity_versions
        SET publication_state='published', published_at=now() WHERE id=:id
    """), {"id": version_id})
    await session.execute(text("""
        UPDATE inha_policy.opportunities SET current_version_id=:version,
          lifecycle_status='active', merged_into_id=NULL, updated_at=now()
        WHERE id=:opportunity
    """), {"version": version_id, "opportunity": row["opportunity_id"]})
    await _audit(session, admin, "published", "opportunity_version", str(version_id),
                 before={"publication_state": row["publication_state"]},
                 after={"publication_state": "published"}, reason=reason)
    try:
        await session.commit()
    except DBAPIError as exc:  # the database's publication guards (sources, conflicts, quality)
        await session.rollback()
        raise HTTPException(409, "공개 규칙에 맞지 않아 공개하지 못했습니다. 근거 충돌이나 출처 문제를 먼저 해결해야 합니다. "
                                 f"(데이터베이스 메시지: {_db_message(exc)})") from exc
    return RedirectResponse(with_message(f"/admin/opportunities/{row['opportunity_id']}", "published"),
                            status_code=303)


@router.post("/opportunities/{opportunity_id}/unpublish")
async def unpublish(opportunity_id: uuid.UUID, request: Request, session: Session,
                    admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    before = (await session.execute(text("""
        SELECT current_version_id, lifecycle_status FROM inha_policy.opportunities
        WHERE id=:id FOR UPDATE
    """), {"id": opportunity_id})).mappings().one_or_none()
    if before is None:
        raise HTTPException(404)
    await session.execute(text("""
        UPDATE inha_policy.opportunities SET current_version_id=NULL,
          lifecycle_status='inactive', updated_at=now() WHERE id=:id
    """), {"id": opportunity_id})
    await _audit(session, admin, "unpublished", "opportunity", str(opportunity_id),
                 before=dict(before), after={"current_version_id": None, "lifecycle_status": "inactive"},
                 reason=reason)
    await session.commit()
    return RedirectResponse(with_message(f"/admin/opportunities/{opportunity_id}", "unpublished"), status_code=303)


@router.post("/opportunities/{opportunity_id}/overrides")
async def set_override(opportunity_id: uuid.UUID, request: Request, session: Session,
                       admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    path = str(form.get("field_path", "")).strip()
    reason = _reason(form)
    if not path.startswith("/"):
        raise HTTPException(422, "고칠 항목(field_path)을 고르세요.")
    value = _override_value(form)
    exists = (await session.execute(text(
        "SELECT EXISTS (SELECT 1 FROM inha_policy.opportunities WHERE id=:id)"
    ), {"id": opportunity_id})).scalar_one()
    if not exists:
        raise HTTPException(404, "장학을 찾을 수 없습니다.")
    override_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.manual_overrides
          (id, opportunity_id, field_path, scope_key, action, value_json, actor_id, reason)
        VALUES (:id, :opportunity, :path, :scope, 'set', CAST(:value AS jsonb), :actor, :reason)
    """), {"id": override_id, "opportunity": opportunity_id, "path": path,
             "scope": str(form.get("scope_key") or "main"), "value": json.dumps(value),
             "actor": admin["id"], "reason": reason})
    await session.execute(text("""
        INSERT INTO inha_policy.change_events
          (event_kind, opportunity_id, opportunity_version_id, previous_version_id,
           edit_kind, visible_after, observed_at, payload)
        SELECT 'updated', id, current_version_id, current_version_id, 'other', true, now(),
               jsonb_build_object('manual_override_id', CAST(:override AS text),
                                  'field_path', CAST(:path AS text))
        FROM inha_policy.opportunities WHERE id=:opportunity AND current_version_id IS NOT NULL
    """), {"override": override_id, "path": path, "opportunity": opportunity_id})
    await _audit(session, admin, "manual_override_set", "opportunity", str(opportunity_id),
                 after={"override_id": str(override_id), "field_path": path, "value": value}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message(f"/admin/opportunities/{opportunity_id}", "override_set"), status_code=303)


@router.post("/overrides/{override_id}/remove")
async def remove_override(override_id: uuid.UUID, request: Request, session: Session,
                          admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    original = (await session.execute(text("""
        SELECT opportunity_id, field_path, scope_key FROM inha_policy.manual_overrides
        WHERE id=:id
    """), {"id": override_id})).mappings().one_or_none()
    if original is None:
        raise HTTPException(422, "취소할 수정 기록을 찾을 수 없습니다.")
    removal_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.manual_overrides
          (id, opportunity_id, field_path, scope_key, action, supersedes_override_id,
           actor_id, reason)
        VALUES (:id, :opportunity, :path, :scope, 'remove', :original, :actor, :reason)
    """), {"id": removal_id, "opportunity": original["opportunity_id"],
             "path": original["field_path"], "scope": original["scope_key"],
             "original": override_id, "actor": admin["id"], "reason": reason})
    await session.execute(text("""
        INSERT INTO inha_policy.change_events
          (event_kind, opportunity_id, opportunity_version_id, previous_version_id,
           edit_kind, visible_after, observed_at, payload)
        SELECT 'updated', id, current_version_id, current_version_id, 'other', true, now(),
               jsonb_build_object('manual_override_removed_id', CAST(:removal AS text),
                                  'field_path', CAST(:path AS text))
        FROM inha_policy.opportunities WHERE id=:opportunity AND current_version_id IS NOT NULL
    """), {"removal": removal_id, "path": original["field_path"],
             "opportunity": original["opportunity_id"]})
    await _audit(session, admin, "manual_override_removed", "opportunity",
                 str(original["opportunity_id"]), after={"removal_id": str(removal_id)}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message(_next_path(form, "/admin/table/manual-overrides"), "override_removed"),
                            status_code=303)


async def _merge_opportunities(session: AsyncSession, admin: dict[str, Any], loser_id: uuid.UUID,
                               winner_id: uuid.UUID, reason: str,
                               proposal_id: uuid.UUID | None = None,
                               review_id: uuid.UUID | None = None) -> None:
    if loser_id == winner_id:
        raise HTTPException(422, "같은 장학끼리는 병합할 수 없습니다. 대표로 남길 다른 장학의 ID를 넣으세요.")
    rows = (await session.execute(text("""
        SELECT id, lifecycle_status FROM inha_policy.opportunities
        WHERE id IN (:loser, :winner) ORDER BY id FOR UPDATE
    """), {"loser": loser_id, "winner": winner_id})).mappings().all()
    states = {row["id"]: row["lifecycle_status"] for row in rows}
    if loser_id not in states or winner_id not in states:
        raise HTTPException(404, "입력한 ID에 해당하는 장학이 없습니다. 대표 장학 상세 화면 아래쪽의 '장학 ID'를 "
                                 "그대로 복사해 넣으세요.")
    if states[loser_id] == "merged":
        raise HTTPException(409, "이 장학은 이미 다른 장학에 병합되어 있습니다. 바꾸려면 먼저 병합을 취소하세요.")
    if states[winner_id] == "merged":
        raise HTTPException(409, "대표로 고른 장학이 이미 다른 장학에 병합되어 있습니다. 그 장학이 병합된 "
                                 "대표 장학을 고르세요.")
    decision_id = uuid.uuid4()
    now = (await session.execute(text("SELECT clock_timestamp()"))).scalar_one()
    await session.execute(text("""
        INSERT INTO inha_policy.identity_decisions
          (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
           supersedes_decision_id, identity_basis, same_program_verified, same_cycle_verified,
           same_scope_verified, amendment_kind, rule_version, decision_reason, actor_kind,
           actor_id, observed_at)
        VALUES (:id, 'merge', 'confirmed', :winner, :loser, :proposal, 'human_verified',
                true, true, true, 'none', 'identity-admin-1.0', :reason, 'human', :actor, :observed)
    """), {"id": decision_id, "winner": winner_id, "loser": loser_id, "reason": reason,
             "actor": str(admin["id"]), "observed": now, "proposal": proposal_id})
    await session.execute(text("""
        UPDATE inha_policy.opportunities SET lifecycle_status='merged', merged_into_id=:winner,
          last_identity_decision_id=:decision, updated_at=now() WHERE id=:loser
    """), {"winner": winner_id, "decision": decision_id, "loser": loser_id})
    if review_id is not None:
        await session.execute(text("""
            UPDATE inha_policy.review_items SET status='resolved', resolution_note=:reason,
              resolved_at=now(), updated_at=now() WHERE id=:id AND status IN ('open','in_review')
        """), {"id": review_id, "reason": reason})
    await _audit(session, admin, "merged", "opportunity", str(loser_id),
                 before={"lifecycle_status": states[loser_id]},
                 after={"merged_into_id": str(winner_id), "decision_id": str(decision_id)}, reason=reason)
    await session.commit()


@router.post("/opportunities/{loser_id}/merge/{winner_id}")
async def merge(loser_id: uuid.UUID, winner_id: uuid.UUID, request: Request, session: Session,
                admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    await _merge_opportunities(session, admin, loser_id, winner_id, _reason(form))
    return RedirectResponse(with_message(f"/admin/opportunities/{winner_id}", "merged"), status_code=303)


@router.post("/opportunities/{loser_id}/merge")
async def merge_from_form(loser_id: uuid.UUID, request: Request, session: Session,
                          admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    try:
        winner_id = uuid.UUID(str(form.get("winner_id", "")))
        proposal_id = uuid.UUID(str(form["proposal_id"])) if form.get("proposal_id") else None
        review_id = uuid.UUID(str(form["review_id"])) if form.get("review_id") else None
    except ValueError as exc:
        raise HTTPException(422, "대표 장학 ID 형식이 맞지 않습니다. 대표 장학 상세 화면 아래쪽의 '장학 ID'를 "
                                 "그대로 복사해 넣으세요.") from exc
    await _merge_opportunities(session, admin, loser_id, winner_id, _reason(form),
                               proposal_id, review_id)
    return RedirectResponse(with_message(_next_path(form, f"/admin/opportunities/{winner_id}"), "merged"),
                            status_code=303)


@router.post("/opportunities/{opportunity_id}/unmerge")
async def unmerge(opportunity_id: uuid.UUID, request: Request, session: Session,
                  admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    row = (await session.execute(text("""
        SELECT merged_into_id, last_identity_decision_id FROM inha_policy.opportunities
        WHERE id=:id AND lifecycle_status='merged' FOR UPDATE
    """), {"id": opportunity_id})).mappings().one_or_none()
    if row is None:
        raise HTTPException(422, "병합된 장학이 아니어서 병합을 취소할 수 없습니다.")
    decision_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.identity_decisions
          (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
           reverses_decision_id, identity_basis, amendment_kind, rule_version,
           decision_reason, actor_kind, actor_id, observed_at)
        VALUES (:id, 'undo', 'confirmed', :opportunity, :other, :reverses,
                'human_verified', 'none', 'identity-admin-1.0', :reason,
                'human', :actor, now())
    """), {"id": decision_id, "opportunity": opportunity_id, "other": row["merged_into_id"],
             "reverses": row["last_identity_decision_id"], "reason": reason,
             "actor": str(admin["id"])})
    await session.execute(text("""
        UPDATE inha_policy.opportunities SET lifecycle_status='active', merged_into_id=NULL,
          last_identity_decision_id=:decision, updated_at=now() WHERE id=:id
    """), {"decision": decision_id, "id": opportunity_id})
    await _audit(session, admin, "merge_reverted", "opportunity", str(opportunity_id),
                 before={"merged_into_id": str(row["merged_into_id"])},
                 after={"lifecycle_status": "active", "decision_id": str(decision_id)}, reason=reason)
    await session.commit()
    return RedirectResponse(with_message(f"/admin/opportunities/{opportunity_id}", "unmerged"), status_code=303)


@router.post("/opportunities/{opportunity_id}/split")
async def split_source(opportunity_id: uuid.UUID, request: Request, session: Session,
                       admin: Reviewer) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    try:
        notice_version_id = uuid.UUID(str(form.get("notice_version_id", "")))
        target_item_index = int(str(form.get("target_item_index", "0")))
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "분리할 공고와 항목 번호(0 이상)가 필요합니다.") from exc
    if target_item_index < 0:
        raise HTTPException(422, "항목 번호는 0 이상이어야 합니다.")
    source = (await session.execute(text("""
        SELECT nv.notice_id, nv.crawl_run_id, ov.title, o.program_key, ovs.extraction_run_id
        FROM inha_policy.opportunity_version_sources ovs
        JOIN inha_policy.opportunity_versions ov ON ov.id=ovs.opportunity_version_id
        JOIN inha_policy.opportunities o ON o.id=ov.opportunity_id
        JOIN inha_policy.notice_versions nv ON nv.id=ovs.notice_version_id
        WHERE ov.opportunity_id=:opportunity AND nv.id=:notice_version
        ORDER BY ov.version_no DESC LIMIT 1
    """), {"opportunity": opportunity_id,
             "notice_version": notice_version_id})).mappings().one_or_none()
    if source is None:
        raise HTTPException(404, "이 공고 버전은 이 장학의 출처가 아닙니다.")
    item_count = (await session.execute(text("""
        SELECT jsonb_array_length(parsed_output->'opportunities')
        FROM inha_policy.extraction_runs WHERE id=:id
    """), {"id": source["extraction_run_id"]})).scalar_one_or_none()
    if item_count is not None and target_item_index >= item_count:
        raise HTTPException(422, f"AI 추출 결과에는 항목이 {item_count}개뿐입니다(0부터 {item_count - 1}까지). "
                                 "항목 번호를 확인하세요.")
    new_opportunity_id = uuid.uuid4()
    decision_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.opportunities (id, program_key, lifecycle_status)
        VALUES (:id, :program_key, 'active')
    """), {"id": new_opportunity_id,
             "program_key": f"{source['program_key'] or 'split'}:{str(new_opportunity_id)[:8]}"})
    await session.execute(text("""
        INSERT INTO inha_policy.identity_decisions
          (id, decision_kind, decision_status, opportunity_id, other_opportunity_id,
           notice_id, source_notice_version_id, identity_basis, same_program_verified,
           same_cycle_verified, same_scope_verified, amendment_kind, update_scope_mode,
           before_state, after_state, rule_version, decision_reason, actor_kind,
           actor_id, observed_at)
        VALUES (:id, 'split', 'confirmed', :original, :new, :notice, :notice_version,
                'human_verified', true, true, true, 'none', 'replacement',
                jsonb_build_object('opportunity_id', CAST(:original AS text)),
                jsonb_build_object('new_opportunity_id', CAST(:new AS text),
                                   'target_item_index', CAST(:item AS integer)),
                'identity-admin-1.0', :reason, 'human', :actor, now())
    """), {"id": decision_id, "original": opportunity_id, "new": new_opportunity_id,
             "notice": source["notice_id"], "notice_version": notice_version_id,
             "item": target_item_index, "reason": reason, "actor": str(admin["id"])})
    await session.execute(text("""
        UPDATE inha_policy.opportunities SET last_identity_decision_id=:decision,
          updated_at=now() WHERE id IN (:original, :new)
    """), {"decision": decision_id, "original": opportunity_id, "new": new_opportunity_id})
    job_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.crawl_jobs
          (id, crawl_run_id, stage, job_key, notice_id, notice_version_id, payload)
        VALUES (:id, :run, 'structure', :key, :notice, :notice_version,
                CAST(:payload AS jsonb))
    """), {"id": job_id, "run": source["crawl_run_id"],
             "key": f"split:{decision_id}", "notice": source["notice_id"],
             "notice_version": notice_version_id,
             "payload": json.dumps({"forced_opportunity_id": str(new_opportunity_id),
                                      "identity_decision_id": str(decision_id),
                                      "target_item_index": target_item_index})})
    await session.execute(text("""
        INSERT INTO inha_policy.review_items
          (review_kind, entity_type, entity_id, opportunity_id, payload)
        VALUES ('identity_uncertain', 'opportunity', :entity, :new, CAST(:payload AS jsonb))
    """), {"entity": new_opportunity_id, "new": new_opportunity_id,
             "payload": json.dumps({"operation": "split", "original_opportunity_id": str(opportunity_id),
                                      "notice_version_id": str(notice_version_id),
                                      "target_item_index": target_item_index,
                                      "structure_job_id": str(job_id)})})
    await _audit(session, admin, "split_queued", "opportunity", str(opportunity_id),
                 before={"notice_version_id": str(notice_version_id)},
                 after={"new_opportunity_id": str(new_opportunity_id),
                        "identity_decision_id": str(decision_id), "job_id": str(job_id)},
                 reason=reason)
    await session.commit()
    return RedirectResponse(with_message(f"/admin/opportunities/{opportunity_id}", "split_queued"), status_code=303)


SETTING_GROUPS = (
    ("llm", "기본 AI · 이미지 전사",
     "공고 이미지와 스캔 페이지를 글로 옮겨 적는 모델입니다. "
     "아래 AI 추출·교차 확인·AI 2차 검토에서 비워 둔 항목(API 종류·주소·key)은 이 설정을 씁니다."),
    ("extraction", "AI 추출", "공고에서 장학 정보를 뽑아 구조화하는 모델입니다. 비워 둔 항목은 위 기본 AI 설정을 씁니다."),
    ("crosscheck", "교차 확인", "장학이 여럿이거나 긴 공고를 다른 모델로 한 번 더 추출하고, 결과가 다르면 이 모델이 원문과 대조해 합칩니다. "
     "모델을 비우면 하지 않습니다. 비워 둔 API 종류·주소·key는 위 기본 AI 설정을 씁니다."),
    ("review", "AI 2차 검토", "품질·동일성·정정 후보처럼 판단이 필요한 항목을 사람에게 보내기 전에 이 모델이 원문과 대조해 한 번 더 봅니다. "
     "확신하면 직접 닫거나 고치고(품질 항목이면 공개까지), 아니면 판단 근거를 붙여 검토함에 올립니다. "
     "모델을 비우면 꺼져 모든 항목이 바로 검토함에 올라옵니다. 비워 둔 API 종류·주소·key는 위 기본 AI 설정을 씁니다."),
    ("embedding", "임베딩(검색 색인)", "장학 검색에 쓰는 색인을 만드는 모델입니다. 바꾸면 모든 장학의 색인을 다시 만들어야 합니다."),
    ("publishing", "공개", "품질이 '완전'인 결과를 사람 확인 없이 바로 공개 API에 내보낼지 정합니다."),
)
SETTING_INPUTS: dict[str, dict[str, Any]] = {
    "llm.provider": {"label": "API 종류", "kind": "choice",
                     "choices": ["openai_compatible", "openai", "azure_openai", "anthropic", "google"]},
    "llm.base_url": {"label": "API 주소(endpoint)", "kind": "url"},
    "llm.model": {"label": "모델", "kind": "text"},
    "llm.timeout_seconds": {"label": "요청 제한 시간(초)", "kind": "number", "min": 10, "max": 3600, "step": 1},
    "llm.max_retries": {"label": "재시도 횟수", "kind": "number", "min": 0, "max": 10, "step": 1},
    "llm.stream": {"label": "스트리밍 (긴 요청이 끊길 때 켜기)", "kind": "bool"},
    "extraction.provider": {"label": "API 종류", "kind": "choice",
                            "choices": ["openai_compatible", "openai", "azure_openai", "anthropic", "google"]},
    "extraction.base_url": {"label": "API 주소(endpoint)", "kind": "url"},
    "extraction.model": {"label": "모델", "kind": "text"},
    "extraction.reasoning_effort": {"label": "추론(thinking) 강도", "kind": "choice", "choices": ["low", "medium", "high"],
                                    "labels": {"low": "low — 빠름", "medium": "medium", "high": "high — 꼼꼼함, 느림"}},
    "llm.max_output_tokens": {"label": "출력 한도(토큰)", "kind": "number", "min": 1024, "max": 1000000,
                              "step": 1, "group": "extraction"},
    "crosscheck.provider": {"label": "API 종류", "kind": "choice",
                            "choices": ["openai_compatible", "openai", "azure_openai", "anthropic", "google"]},
    "crosscheck.base_url": {"label": "API 주소(endpoint)", "kind": "url"},
    "extraction.crosscheck_model": {"label": "모델 (비우면 교차 확인 안 함)", "kind": "text", "group": "crosscheck"},
    "review.provider": {"label": "API 종류", "kind": "choice",
                        "choices": ["openai_compatible", "openai", "azure_openai", "anthropic", "google"]},
    "review.base_url": {"label": "API 주소(endpoint)", "kind": "url"},
    "review.model": {"label": "검토 모델 (비우면 AI 2차 검토 끔)", "kind": "text"},
    "review.reasoning_effort": {"label": "추론(thinking) 강도", "kind": "choice", "choices": ["low", "medium", "high"],
                                "labels": {"low": "low — 빠름", "medium": "medium", "high": "high — 꼼꼼함, 느림"}},
    "embedding.provider": {"label": "API 종류 (외부 API 또는 내장 모델)", "kind": "choice",
                           "choices": ["onnx", "openai_compatible", "openai", "azure_openai", "cohere", "voyage",
                                       "google"],
                           "labels": {"onnx": "onnx — 내장 모델 (이 서버 CPU)"}},
    "embedding.base_url": {"label": "API 주소(endpoint)", "kind": "url"},
    "embedding.model": {"label": "모델 (내장이면 아래 목록 중 하나)", "kind": "text", "suggest": "onnx_models"},
    "embedding.dimensions": {"label": "차원(벡터 크기)", "kind": "number", "min": 1, "max": 8192, "step": 1},
    "publishing.auto_publish": {"label": "품질 '완전'이면 자동 공개", "kind": "bool"},
}
for _key, _spec in SETTING_INPUTS.items():
    _spec.setdefault("group", _key.split(".", 1)[0])
SECRET_FIELDS = {"llm.api_key": ("API key", "llm_api_key"),
                 "extraction.api_key": ("API key", "extraction_llm_api_key"),
                 "crosscheck.api_key": ("API key", "crosscheck_llm_api_key"),
                 "review.api_key": ("API key", "review_llm_api_key"),
                 "embedding.api_key": ("API key", "embedding_api_key")}


def _secret_status(active: dict[str, Any]) -> list[dict[str, Any]]:
    """Each secret as the providers will use it: admin-stored, else .env, masked either way."""
    rows = []
    for key, (label, attribute) in SECRET_FIELDS.items():
        env_value = getattr(settings, attribute)
        stored = active.get(key)
        if stored:
            rows.append({"key": key, "label": label, "masked": stored["masked_value"], "source": "admin",
                         "updated_at": stored["created_at"]})
        elif env_value is not None and env_value.get_secret_value():
            rows.append({"key": key, "label": label, "masked": mask_secret(env_value.get_secret_value()),
                         "source": "environment", "updated_at": None})
        elif key != "llm.api_key" and (active.get("llm.api_key") or settings.llm_api_key):
            rows.append({"key": key, "label": label, "masked": "기본 AI의 API key를 같이 씀", "source": "fallback",
                         "updated_at": None})
        else:
            rows.append({"key": key, "label": label, "masked": "", "source": "missing", "updated_at": None})
    return rows


@router.get("/settings", response_class=HTMLResponse)
async def ai_settings(request: Request, session: Session, admin: Admin) -> HTMLResponse:
    effective = await effective_configuration(session)
    stored_secrets = {row["secret_key"]: dict(row) for row in (await session.execute(text("""
        SELECT secret_key, masked_value, key_id, created_at
        FROM inha_policy.encrypted_secrets WHERE is_active ORDER BY secret_key
    """))).mappings().all()}
    wanted = (f"{effective['embedding.provider']['value']}:{effective['embedding.model']['value']}:"
              f"{int(effective['embedding.dimensions']['value'])}:{CHUNKER_VERSION}")
    profiles = {row["profile_key"]: dict(row) for row in (await session.execute(text(
        "SELECT id, profile_key, is_active FROM inha_policy.embedding_profiles"))).mappings().all()}
    active = next((row for row in profiles.values() if row["is_active"]), None)
    current = profiles.get(wanted)
    embedding_status = {
        "wanted_key": wanted, "active": active, "current": current,
        "coverage": await coverage(session, current["id"]) if current else None,
        "onnx_models": ONNX_MODELS,
    }
    # The latest connection test per capability, so its result stays visible after the redirect.
    tests = {row["entity_id"]: dict(row) for row in (await session.execute(text("""
        SELECT DISTINCT ON (entity_id) entity_id, after_state, created_at
        FROM inha_policy.audit_logs WHERE action='provider_connection_tested'
        ORDER BY entity_id, created_at DESC
    """))).mappings().all()}
    return templates.TemplateResponse(request, "settings.html",
                                      _context(request, admin, effective=effective, inputs=SETTING_INPUTS,
                                               embedding_status=embedding_status,
                                               groups=SETTING_GROUPS,
                                               secrets={row["key"].split(".", 1)[0]: row
                                                        for row in _secret_status(stored_secrets)},
                                               allowed=RUNTIME_KEYS, tests=tests))


def _coerce_setting(key: str, raw: str) -> Any:
    spec = SETTING_INPUTS.get(key, {"kind": "text"})
    name = f"{spec.get('label', key)}({key})"
    if spec["kind"] == "choice" and raw not in spec["choices"]:
        raise HTTPException(422, f"{name}: {', '.join(spec['choices'])} 중 하나를 고르세요.")
    if spec["kind"] == "url" and not re.fullmatch(r"https?://[^\s/$.?#][^\s]*", raw):
        raise HTTPException(422, f"{name}: http:// 또는 https://로 시작하는 주소를 넣으세요.")
    annotation = Settings.model_fields[RUNTIME_KEYS[key]].annotation
    limits = f" ({spec['min']}~{spec['max']} 사이의 숫자)" if spec["kind"] == "number" else ""
    try:
        value = TypeAdapter(annotation).validate_python(raw)
    except ValidationError as exc:
        raise HTTPException(422, f"{name}: 값이 올바르지 않습니다{limits}. "
                                 f"[{exc.errors()[0]['msg']}]") from exc
    # The form's min/max are only a browser hint; a value outside them must not reach the workers.
    if spec["kind"] == "number" and not spec["min"] <= value <= spec["max"]:
        raise HTTPException(422, f"{name}: 허용 범위를 벗어났습니다{limits}.")
    return value


async def _remove_setting_override(session: AsyncSession, admin: dict[str, Any], key: str) -> None:
    before = (await session.execute(text(
        "DELETE FROM inha_policy.runtime_settings WHERE setting_key=:key RETURNING value_json"
    ), {"key": key})).scalar_one_or_none()
    if before is not None:
        await _audit(session, admin, "setting_override_removed", "runtime_setting", key,
                     before={"value": before}, after=None, reason="restore environment/default")


@router.post("/settings")
async def update_setting(request: Request, session: Session, admin: Admin) -> RedirectResponse:
    """Save an override; an empty value, or the .env/default value itself, removes the override."""
    form = await _form(request, admin)
    key = str(form.get("key", ""))
    if key not in RUNTIME_KEYS:
        raise HTTPException(422, "이 화면에서 바꿀 수 없는 설정입니다.")
    raw = str(form.get("value", "")).strip()
    base_value = getattr(settings, RUNTIME_KEYS[key])
    value = _coerce_setting(key, raw) if raw else None
    if value is None or value == base_value:
        await _remove_setting_override(session, admin, key)
        queued = await _after_embedding_change(session, admin, key)
        await session.commit()
        return RedirectResponse(_settings_result(key, "setting_reset", queued), status_code=303)
    before = (await session.execute(text(
        "SELECT value_json FROM inha_policy.runtime_settings WHERE setting_key=:key"
    ), {"key": key})).scalar_one_or_none()
    await session.execute(text("""
        INSERT INTO inha_policy.runtime_settings (setting_key, value_json, updated_by)
        VALUES (:key, CAST(:value AS jsonb), :actor)
        ON CONFLICT (setting_key) DO UPDATE SET value_json=EXCLUDED.value_json,
          updated_by=EXCLUDED.updated_by, updated_at=now()
    """), {"key": key, "value": json.dumps(value), "actor": admin["id"]})
    await _audit(session, admin, "setting_updated", "runtime_setting", key,
                 before={"value": before}, after={"value": value}, reason="admin override")
    queued = await _after_embedding_change(session, admin, key)
    await session.commit()
    return RedirectResponse(_settings_result(key, "setting_saved", queued), status_code=303)


def _settings_result(key: str, done: str, queued: int | None) -> str:
    if queued is not None:
        return with_message("/admin/settings", "embedding_setting_saved", n=queued)
    return with_message("/admin/settings", done)


EMBEDDING_KEYS = {"embedding.provider", "embedding.model", "embedding.dimensions"}


async def _set_override(session: AsyncSession, admin: dict[str, Any], key: str, value: Any) -> None:
    await session.execute(text("""
        INSERT INTO inha_policy.runtime_settings (setting_key, value_json, updated_by)
        VALUES (:key, CAST(:value AS jsonb), :actor)
        ON CONFLICT (setting_key) DO UPDATE SET value_json=EXCLUDED.value_json,
          updated_by=EXCLUDED.updated_by, updated_at=now()
    """), {"key": key, "value": json.dumps(value), "actor": admin["id"]})
    await _audit(session, admin, "setting_updated", "runtime_setting", key, after={"value": value},
                 reason="derived from the embedding model")


async def _after_embedding_change(session: AsyncSession, admin: dict[str, Any], key: str) -> int | None:
    """A different embedding model or size invalidates every vector: re-embed everything.

    For an internal (onnx) model the dimension follows the model. An impossible combination is
    left unqueued; the settings page shows it. The worker switches search to the new index once
    it is complete, so search keeps working on the old one meanwhile.
    """
    if key not in EMBEDDING_KEYS:
        return None
    effective = await effective_configuration(session)
    provider = effective["embedding.provider"]["value"]
    model = effective["embedding.model"]["value"]
    if provider == "onnx":
        spec = ONNX_MODELS.get(model)
        if spec is None:
            return None
        dimensions = int(effective["embedding.dimensions"]["value"])
        if dimensions != spec.dimensions and dimensions not in spec.truncatable_to:
            await _set_override(session, admin, "embedding.dimensions", spec.dimensions)
    return await _queue_embeddings(session, admin, f"embedding settings changed ({key})")


@router.post("/settings/{key}/delete")
async def delete_setting(key: str, request: Request, session: Session, admin: Admin) -> RedirectResponse:
    await _form(request, admin)
    await _remove_setting_override(session, admin, key)
    await session.commit()
    return RedirectResponse(with_message("/admin/settings", "setting_reset"), status_code=303)


@router.post("/secrets")
async def update_secret(request: Request, session: Session, admin: Admin) -> RedirectResponse:
    """Store a new secret version; an empty value retires the stored one so .env applies again."""
    form = await _form(request, admin)
    key = str(form.get("key", ""))
    value = str(form.get("value", "")).strip()
    if key not in SECRET_FIELDS:
        raise HTTPException(422, "알 수 없는 API key 항목입니다.")
    if not value:
        retired = (await session.execute(text("""
            UPDATE inha_policy.encrypted_secrets SET is_active=false, revoked_at=now()
            WHERE secret_key=:key AND is_active RETURNING version_no, masked_value
        """), {"key": key})).mappings().one_or_none()
        if retired is not None:
            await _audit(session, admin, "secret_override_removed", "encrypted_secret", key,
                         before={"version": retired["version_no"], "masked": retired["masked_value"]},
                         after=None, reason="restore environment secret")
        await session.commit()
        return RedirectResponse(with_message("/admin/settings", "secret_removed"), status_code=303)
    if not settings.master_key or not settings.master_key.get_secret_value():
        return RedirectResponse(with_message("/admin/settings", "master_key_missing", problem=True), status_code=303)
    version = (await session.execute(text("""
        SELECT coalesce(max(version_no), 0)+1 FROM inha_policy.encrypted_secrets
        WHERE secret_key=:key
    """), {"key": key})).scalar_one()
    await session.execute(text("""
        UPDATE inha_policy.encrypted_secrets SET is_active=false, revoked_at=now()
        WHERE secret_key=:key AND is_active
    """), {"key": key})
    encrypted = encrypt_secret(value, settings.master_key.get_secret_value(), key)
    await session.execute(text("""
        INSERT INTO inha_policy.encrypted_secrets
          (secret_key, version_no, ciphertext, key_id, fingerprint, masked_value, created_by)
        VALUES (:key, :version, :ciphertext, 'env-master-v1', :fingerprint, :masked, :actor)
    """), {"key": key, "version": version, "ciphertext": encrypted,
             "fingerprint": hashlib.sha256(value.encode()).hexdigest(),
             "masked": mask_secret(value), "actor": admin["id"]})
    await _audit(session, admin, "secret_rotated", "encrypted_secret", key,
                 after={"version": version, "masked": mask_secret(value)}, reason="credential rotation")
    await session.commit()
    return RedirectResponse(with_message("/admin/settings", "secret_saved"), status_code=303)


@router.post("/sources/{source_id}/proxy")
async def update_source_proxy(source_id: uuid.UUID, request: Request, session: Session,
                              admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    value = str(form.get("proxy_url", "")).strip()
    reason = _reason(form)
    source_key = (await session.execute(text(
        "SELECT source_key FROM inha_policy.sources WHERE id=:id"
    ), {"id": source_id})).scalar_one_or_none()
    if source_key is None:
        raise HTTPException(404)
    if not settings.master_key or not settings.master_key.get_secret_value():
        return RedirectResponse(with_message("/admin/table/sources", "master_key_missing", problem=True),
                                status_code=303)
    secret_key = f"crawl.proxy.{source_key}"
    if value:
        try:
            validate_proxy_url(value)
        except ValueError as exc:
            raise HTTPException(422, f"proxy 주소가 올바르지 않습니다: {exc}") from exc
        version = (await session.execute(text("""
            SELECT coalesce(max(version_no), 0)+1 FROM inha_policy.encrypted_secrets
            WHERE secret_key=:key
        """), {"key": secret_key})).scalar_one()
        await session.execute(text("""
            UPDATE inha_policy.encrypted_secrets SET is_active=false, revoked_at=now()
            WHERE secret_key=:key AND is_active
        """), {"key": secret_key})
        encrypted = encrypt_secret(value, settings.master_key.get_secret_value(), secret_key)
        masked = mask_proxy_url(value)
        await session.execute(text("""
            INSERT INTO inha_policy.encrypted_secrets
              (secret_key, version_no, ciphertext, key_id, fingerprint, masked_value, created_by)
            VALUES (:key, :version, :ciphertext, 'env-master-v1', :fingerprint, :masked, :actor)
        """), {"key": secret_key, "version": version, "ciphertext": encrypted,
                 "fingerprint": hashlib.sha256(value.encode()).hexdigest(),
                 "masked": masked, "actor": admin["id"]})
        action, after = "source_proxy_updated", {"masked_proxy": masked}
    else:
        await session.execute(text("""
            UPDATE inha_policy.encrypted_secrets SET is_active=false, revoked_at=now()
            WHERE secret_key=:key AND is_active
        """), {"key": secret_key})
        action, after = "source_proxy_removed", {"fallback": "CRAWL_PROXY_URL or direct"}
    await _audit(session, admin, action, "source", str(source_id), after=after, reason=reason)
    await session.commit()
    return RedirectResponse(with_message("/admin/table/sources", "source_proxy_saved" if value else "source_proxy_removed"),
                            status_code=303)


@router.post("/settings/test/{capability}")
async def test_provider(capability: str, request: Request, session: Session,
                        admin: Admin) -> RedirectResponse:
    await _form(request, admin)
    if capability not in {"llm", "embedding"}:
        raise HTTPException(404)
    resolved = await resolved_settings(session)
    config = await effective_configuration(session)
    registry = ProviderRegistry(resolved)
    model = config[f"{capability}.model"]["value"]
    try:
        provider = registry.llm() if capability == "llm" else registry.embedding()
        result = await provider.test_connection(model)
    except Exception as exc:  # a failed test is a result to show, not a server error
        result = {"ok": False, "model": model, "error_code": exc.__class__.__name__,
                  "error": str(exc)[:1000]}
    await _audit(session, admin, "provider_connection_tested", "ai_provider", capability,
                 after=result, reason="connection test")
    await session.commit()
    return RedirectResponse(with_message("/admin/settings#test", "test_done" if result.get("ok") else "test_failed",
                                         problem=not result.get("ok")), status_code=303)


@router.get("/search-debug", response_class=HTMLResponse)
async def search_debug(request: Request, session: Session, admin: Viewer,
                       q: str = "") -> HTMLResponse:
    rows: list[dict[str, Any]] = []
    vector_used = False
    vector_error: str | None = None
    if q:
        profile = (await session.execute(text("""
            SELECT id, model_id, dimensions, config FROM inha_policy.embedding_profiles
            WHERE is_active LIMIT 1
        """))).mappings().one_or_none()
        vector_literal: str | None = None
        if profile:
            try:
                provider = profile["config"].get("provider", "openai")
                vector = (await ProviderRegistry(await resolved_settings(session)).embedding(provider).embed(
                    model=profile["model_id"], texts=[q], dimensions=profile["dimensions"], kind="query"
                ))[0]
                magnitude = math.sqrt(sum(item * item for item in vector))
                if len(vector) != profile["dimensions"] or magnitude == 0:
                    raise ValueError("invalid query embedding dimensions or magnitude")
                vector = [item / magnitude for item in vector]
                vector_literal = "[" + ",".join(str(item) for item in vector) + "]"
                vector_used = True
            except Exception as exc:
                vector_error = str(exc)[:500]
        vector_expression = ("1 - (sc.embedding <=> CAST(:query_vector AS vector))"
                             if vector_literal else "0.0")
        parameters: dict[str, Any] = {"q": q}
        if vector_literal:
            parameters["query_vector"] = vector_literal
        rows = [dict(row) for row in (await session.execute(text(f"""
            SELECT ov.opportunity_id, ov.title AS opportunity_title,
                   ov.publication_state, sc.chunk_text,
                   greatest(similarity(ov.title, :q),
                     ts_rank(sc.search_tsv, plainto_tsquery('simple', :q))) AS lexical_score,
                   {vector_expression} AS vector_score,
                   greatest(similarity(ov.title, :q),
                     ts_rank(sc.search_tsv, plainto_tsquery('simple', :q))) * 0.45
                     + ({vector_expression}) * 0.55 AS final_score
            FROM inha_policy.search_chunks sc
            JOIN inha_policy.opportunity_versions ov ON ov.id=sc.opportunity_version_id
            JOIN inha_policy.embedding_profiles ep ON ep.id=sc.embedding_profile_id
            WHERE ep.is_active AND sc.embedding_status='succeeded'
              AND (sc.evidence_role='current' OR ov.publication_state='draft')
            ORDER BY final_score DESC LIMIT 30
        """), parameters)).mappings().all()]
    from .search import understand_query
    return templates.TemplateResponse(request, "search_debug.html",
                                      _context(request, admin, q=q, rows=rows,
                                               filters=understand_query(q),
                                               vector_used=vector_used,
                                               vector_error=vector_error))


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, session: Session, admin: Admin,
                     reset: dict[str, str] | None = None) -> HTMLResponse:
    rows = (await session.execute(text("""
        SELECT id, email, display_name, role, is_active, password_change_required, last_login_at, created_at
        FROM inha_policy.admin_users WHERE deleted_at IS NULL ORDER BY email
    """))).mappings().all()
    return templates.TemplateResponse(request, "users.html", _context(
        request, admin, rows=rows, created=request.query_params.get("created"),
        deleted=request.query_params.get("deleted"), error=request.query_params.get("error"), reset=reset))


@router.post("/users")
async def create_user(request: Request, session: Session, admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    email = str(form.get("email", "")).strip().lower()
    display_name = str(form.get("display_name", "")).strip()
    role = str(form.get("role", "viewer"))
    password = str(form.get("password", ""))
    problem = ("이메일 형식이 올바르지 않습니다." if "@" not in email else
               "이름을 입력하세요." if not display_name else
               "역할을 다시 고르세요." if role not in ROLE_LEVEL else
               f"임시 비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다." if len(password) < MIN_PASSWORD_LENGTH
               else None)
    if problem is None and (await session.execute(text(
            "SELECT 1 FROM inha_policy.admin_users WHERE email=:email"), {"email": email})).first():
        problem = f"{email} 계정이 이미 있습니다. 비밀번호를 잊었다면 목록에서 '비밀번호 초기화'를 쓰세요."
    if problem:
        return RedirectResponse(f"/admin/users?{urlencode({'error': problem})}", status_code=303)
    user_id = uuid.uuid4()
    await session.execute(text("""
        INSERT INTO inha_policy.admin_users
          (id, email, display_name, password_hash, role, password_change_required)
        VALUES (:id, :email, :name, :password, :role, true)
    """), {"id": user_id, "email": email, "name": display_name,
             "password": passwords.hash(password), "role": role})
    await _audit(session, admin, "admin_user_created", "admin_user", str(user_id),
                 after={"email": email, "display_name": display_name, "role": role},
                 reason="user administration")
    await session.commit()
    return RedirectResponse(f"/admin/users?{urlencode({'created': email})}", status_code=303)


@router.post("/users/{user_id}/reset-password", response_class=HTMLResponse)
async def reset_password(user_id: uuid.UUID, request: Request, session: Session, admin: Admin) -> Response:
    """Give an account a temporary password, shown once here; it must be changed at next sign-in."""
    await _form(request, admin)
    if user_id == admin["id"]:
        return RedirectResponse(f"/admin/users?{urlencode({'error': '자기 비밀번호는 내 계정 화면에서 바꾸세요.'})}",
                                status_code=303)
    temporary = secrets.token_urlsafe(12)
    email = (await session.execute(text("""
        UPDATE inha_policy.admin_users SET password_hash=:hash, password_change_required=true,
          password_changed_at=clock_timestamp(), updated_at=now()
        WHERE id=:id AND deleted_at IS NULL RETURNING email
    """), {"id": user_id, "hash": passwords.hash(temporary)})).scalar_one_or_none()
    if email is None:
        raise HTTPException(404)
    await _audit(session, admin, "admin_password_reset", "admin_user", str(user_id),
                 after={"email": email, "password_change_required": True}, reason="user administration")
    await session.commit()
    return await users_page(request, session, admin, reset={"email": email, "password": temporary})


@router.post("/users/{user_id}/delete")
async def delete_user(user_id: uuid.UUID, request: Request, session: Session, admin: Admin) -> RedirectResponse:
    """Remove an account from the admin: it can no longer sign in and its email can be reused.

    The row stays (audit logs, API keys and overrides refer to it), with the original email in
    deleted_email. You cannot delete yourself or the last remaining admin.
    """
    await _form(request, admin)
    if user_id == admin["id"]:
        return RedirectResponse(f"/admin/users?{urlencode({'error': '자기 계정은 삭제할 수 없습니다.'})}",
                                status_code=303)
    target = (await session.execute(text("""
        SELECT email, role FROM inha_policy.admin_users WHERE id=:id AND deleted_at IS NULL FOR UPDATE
    """), {"id": user_id})).mappings().one_or_none()
    if target is None:
        raise HTTPException(404)
    if target["role"] == "admin":
        others = (await session.execute(text("""
            SELECT count(*) FROM inha_policy.admin_users
            WHERE role='admin' AND is_active AND deleted_at IS NULL AND id<>:id
        """), {"id": user_id})).scalar_one()
        if not others:
            return RedirectResponse(f"/admin/users?{urlencode({'error': '마지막 관리자 계정은 삭제할 수 없습니다. 다른 계정을 관리자로 만든 뒤 삭제하세요.'})}",
                                    status_code=303)
    await session.execute(text("""
        UPDATE inha_policy.admin_users
        SET is_active=false, deleted_at=now(), deleted_email=email,
            email='deleted+' || id::text || '@deleted.invalid', updated_at=now()
        WHERE id=:id
    """), {"id": user_id})
    await _audit(session, admin, "admin_user_deleted", "admin_user", str(user_id),
                 before={"email": target["email"], "role": target["role"]}, after={"deleted": True},
                 reason="user administration")
    await session.commit()
    return RedirectResponse(f"/admin/users?{urlencode({'deleted': target['email']})}", status_code=303)


async def _api_key_rows(session: AsyncSession) -> list[dict[str, Any]]:
    return [dict(row) for row in (await session.execute(text("""
        SELECT k.id, k.name, k.key_prefix, k.rate_limit_per_minute, k.created_at, k.expires_at,
               k.revoked_at, k.last_used_at, k.request_count, u.email AS created_by_email,
               CASE WHEN k.revoked_at IS NOT NULL THEN 'revoked'
                    WHEN k.expires_at IS NOT NULL AND k.expires_at <= now() THEN 'expired'
                    ELSE 'active' END AS state
        FROM inha_policy.api_keys k LEFT JOIN inha_policy.admin_users u ON u.id=k.created_by
        ORDER BY (k.revoked_at IS NULL) DESC, k.created_at DESC
    """))).mappings().all()]


@router.get("/api-keys", response_class=HTMLResponse)
async def api_keys_page(request: Request, session: Session, admin: Admin) -> HTMLResponse:
    return templates.TemplateResponse(request, "api_keys.html", _context(
        request, admin, rows=await _api_key_rows(session), new_key=None,
        key_required=get_settings().api_key_required))


@router.post("/api-keys", response_class=HTMLResponse)
async def create_api_key(request: Request, session: Session, admin: Admin) -> HTMLResponse:
    """Issue a key and show it once; only its SHA-256 and a short prefix are stored."""
    form = await _form(request, admin)
    name = str(form.get("name", "")).strip()
    try:
        rate_limit = int(str(form.get("rate_limit_per_minute") or 120))
        expires_days = int(str(form.get("expires_days") or 0))
    except ValueError as exc:
        raise HTTPException(422, "분당 요청 한도와 유효 기간은 숫자로 넣으세요.") from exc
    if not 1 <= len(name) <= 120 or not 1 <= rate_limit <= 10000 or not 0 <= expires_days <= 3650:
        raise HTTPException(422, "이름(1~120자), 분당 요청 한도(1~10000), 유효 기간(0~3650일)을 확인하세요.")
    key, prefix, digest = generate_key()
    key_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None
    await session.execute(text("""
        INSERT INTO inha_policy.api_keys
          (id, name, key_prefix, key_sha256, rate_limit_per_minute, created_by, expires_at)
        VALUES (:id, :name, :prefix, :digest, :rate, :admin, :expires)
    """), {"id": key_id, "name": name, "prefix": prefix, "digest": digest, "rate": rate_limit,
             "admin": admin["id"], "expires": expires_at})
    await _audit(session, admin, "api_key_created", "api_key", str(key_id),
                 after={"name": name, "prefix": prefix, "rate_limit_per_minute": rate_limit,
                        "expires_at": expires_at}, reason="api key administration")
    await session.commit()
    response = templates.TemplateResponse(request, "api_keys.html", _context(
        request, admin, rows=await _api_key_rows(session),
        new_key={"key": key, "name": name, "prefix": prefix},
        key_required=get_settings().api_key_required))
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api-keys/{key_id}/revoke")
async def revoke_api_key(key_id: uuid.UUID, request: Request, session: Session,
                         admin: Admin) -> RedirectResponse:
    form = await _form(request, admin)
    reason = _reason(form)
    row = (await session.execute(text("""
        UPDATE inha_policy.api_keys SET revoked_at=now(), revoked_by=:admin
        WHERE id=:id AND revoked_at IS NULL RETURNING name, key_prefix
    """), {"id": key_id, "admin": admin["id"]})).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "이미 폐기했거나 없는 key입니다.")
    await _audit(session, admin, "api_key_revoked", "api_key", str(key_id),
                 before={"name": row["name"], "prefix": row["key_prefix"]}, after={"revoked": True},
                 reason=reason)
    await session.commit()
    return RedirectResponse(with_message("/admin/api-keys", "key_revoked"), status_code=303)
