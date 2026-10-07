"""Words the admin pages show staff: status labels, error explanations and result messages.

Staff are not engineers, so database values (``parse_document``, ``retry``, ``needs_review``)
and exception names are shown in Korean; the raw value stays available as a tooltip where it
helps when something has to be passed on to a developer.

Terms used everywhere: 공고 (a board post), 장학 (an opportunity extracted from it), 검토함,
작업 큐, 파싱, AI 추출, AI 2차 검토, 임베딩(검색 색인), 공개 / 공개 전 / 비공개, 버전, 블록, 원문.
"""
from __future__ import annotations

import re
from typing import Any

from markupsafe import Markup
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# label domain -> {raw value: (Korean label, chip class)}
LABELS: dict[str, dict[str, tuple[str, str]]] = {
    "job_stage": {
        "discover_list": ("목록 수집", ""), "fetch_detail": ("상세 수집", ""),
        "download_asset": ("첨부 내려받기", ""), "parse_document": ("파싱", ""),
        "structure": ("AI 추출", ""), "embed": ("임베딩", ""), "publish": ("공개", ""),
        "review": ("AI 2차 검토", ""),
    },
    "job_status": {
        "queued": ("대기", "info"), "running": ("실행 중", "info"), "retry": ("재시도 대기", "warn"),
        "succeeded": ("완료", "ok"), "failed": ("실패", "bad"), "skipped": ("건너뜀", ""),
    },
    "run_status": {
        "queued": ("대기", "info"), "running": ("수집 중", "info"), "succeeded": ("완료", "ok"),
        "partial": ("일부 실패", "warn"), "failed": ("실패", "bad"), "cancelled": ("취소됨", ""),
    },
    "run_mode": {
        "backfill": ("과거 글 수집", ""), "daily_full": ("정기 수집", ""),
        "reconciliation": ("대조 수집", ""), "manual": ("수동 수집", ""),
    },
    "doc_status": {
        "queued": ("대기", "info"), "running": ("처리 중", "info"), "succeeded": ("완료", "ok"),
        "partial": ("일부만 읽음", "warn"), "failed": ("실패", "bad"),
        "unsupported": ("지원하지 않는 형식", "warn"), "encrypted": ("암호 걸린 파일", "warn"),
    },
    "extraction_status": {
        "queued": ("대기", "info"), "running": ("실행 중", "info"), "succeeded": ("완료", "ok"),
        "validation_failed": ("형식 검증 실패", "bad"), "failed": ("실패", "bad"),
        "refused": ("모델이 거부", "bad"), "incomplete": ("응답이 잘림", "bad"),
    },
    "review_status": {
        "ai_pending": ("AI 2차 검토 중", "info"), "open": ("검토 대기", "warn"),
        "in_review": ("검토 중", "warn"), "resolved": ("처리됨", "ok"), "dismissed": ("문제 없음으로 닫힘", ""),
    },
    "review_kind": {
        "revision_candidate": ("정정·연장 후보", "info"), "identity_uncertain": ("동일성 확인", "info"),
        "llm_validation_failed": ("AI 추출 실패", "bad"), "parsing_failed": ("파싱 실패", "bad"),
        "embedding_failed": ("임베딩 실패", "bad"), "date_conflict": ("날짜 충돌", "warn"),
        "amount_conflict": ("금액 충돌", "warn"), "eligibility_conflict": ("자격 충돌", "warn"),
        "ocr_failed": ("판독 실패", "bad"), "privacy_review": ("개인정보 검토", "warn"),
        "other": ("품질 확인", "warn"),
    },
    "publication": {
        "draft": ("공개 전", ""), "published": ("공개됨", "ok"), "rejected": ("반려", "bad"),
    },
    "quality": {
        "complete": ("완전", "ok"), "partial": ("일부 불완전", "warn"), "needs_review": ("검토 필요", "bad"),
    },
    "lifecycle": {
        "active": ("정상", "ok"), "inactive": ("비공개", ""), "merged": ("병합됨", "info"),
    },
    "edit_kind": {
        "initial": ("최초 추출", ""), "in_place_edit": ("원문 수정 반영", ""), "extension": ("기간 연장", "info"),
        "correction": ("정정", "info"), "cancellation": ("취소", "warn"), "reopened": ("재접수", "info"),
        "new_round": ("새 회차", ""), "merge": ("병합", ""), "split": ("분리", ""), "unlink": ("연결 해제", ""),
        "restore": ("복원", ""), "other": ("기타 변경", ""),
    },
    "relation_kind": {
        "original": ("원 공고", ""), "correction": ("정정 공고", "info"), "extension": ("연장 공고", "info"),
        "cancellation": ("취소 공고", "warn"), "supplement": ("보완 공고", "info"),
        "duplicate": ("중복 게시", ""), "reopened": ("재접수 공고", "info"),
    },
    "scope": {
        "included": ("장학 분류", "ok"), "excluded": ("범위 밖", ""), "uncertain": ("범위 판단 보류", "warn"),
    },
    "availability": {
        "available": ("게시 중", "ok"), "uncertain": ("확인 필요", "warn"), "unavailable": ("접근 불가", "bad"),
        "deleted": ("원문 삭제됨", "bad"),
    },
    "asset_collection": {
        "pending": ("내려받는 중", "info"), "complete": ("모두 받음", "ok"), "partial": ("일부 못 받음", "warn"),
    },
    "download": {
        "pending": ("대기", "info"), "succeeded": ("받음", "ok"), "failed": ("실패", "bad"), "skipped": ("건너뜀", ""),
    },
    "window_kind": {
        "application": ("신청", ""), "nomination": ("학교 추천", ""), "document_delivery": ("서류 제출", ""),
        "additional_application": ("추가 신청", ""), "program": ("프로그램", ""), "event": ("행사", ""),
        "interview": ("면접", ""), "result": ("결과 발표", ""), "payment": ("지급", ""), "other": ("기타 일정", ""),
    },
    "benefit_kind": {
        "tuition": ("등록금", ""), "living_cost": ("생활비", ""), "travel": ("교통·여비", ""), "cash": ("현금", ""),
        "in_kind": ("현물", ""), "service": ("서비스", ""), "other": ("기타", ""), "unknown": ("종류 미상", ""),
    },
    "role": {
        "viewer": ("조회", ""), "operator": ("운영", "info"), "reviewer": ("검토", "info"), "admin": ("관리자", "brand"),
    },
    "exchange_status": {"ok": ("성공", "ok"), "error": ("실패", "bad")},
    "exchange_purpose": {
        "extraction": ("AI 추출", ""), "repair": ("JSON 수리", ""), "transcription": ("이미지 전사", ""),
        "other": ("기타", ""),
    },
    "actor_kind": {
        "admin_user": ("관리자", ""), "worker": ("자동 처리", ""), "system": ("시스템", ""),
        "ai_review": ("AI 2차 검토", ""), "human": ("관리자", ""),
    },
    "entity_type": {
        "notice": ("공고", ""), "notice_version": ("공고 버전", ""), "opportunity": ("장학", ""),
        "opportunity_version": ("장학 버전", ""), "review_item": ("검토 항목", ""), "crawl_job": ("작업", ""),
        "crawl_run": ("수집 실행", ""), "document": ("파싱 문서", ""), "admin_user": ("사용자", ""),
        "api_key": ("API key", ""), "runtime_setting": ("AI 설정", ""), "encrypted_secret": ("API key 설정", ""),
        "source": ("수집 대상", ""), "embedding_profile": ("임베딩 프로필", ""), "ai_provider": ("AI 연결", ""),
        "identity_decision": ("동일성 판정", ""),
    },
    "override_action": {"set": ("수정", "brand"), "remove": ("수정 취소", "")},
    "asset_state": {
        "added": ("추가", "ok"), "removed": ("삭제", "bad"), "changed": ("내용 바뀜", "warn"), "unchanged": ("같음", ""),
    },
    "asset_role": {"attachment": ("첨부", ""), "inline_image": ("본문 이미지", ""), "css_image": ("배경 이미지", "")},
    "api_key_state": {"active": ("사용 중", "ok"), "revoked": ("폐기", "bad"), "expired": ("만료", "warn")},
}

ACTOR_NAMES = {
    "crawler": "수집기", "assembler": "장학 생성(자동)", "publisher": "자동 공개", "ai_reviewer": "AI 2차 검토",
    "embedder": "임베딩(자동)", "maintenance": "유지보수 작업",
}

AUDIT_ACTIONS = {
    "notice_version_created": "공고 새 버전 수집", "opportunity_version_created": "장학 버전 생성",
    "auto_published": "자동 공개", "published": "공개", "unpublished": "비공개 전환",
    "review_dismissed_by_ai": "AI가 검토 항목을 문제 없음으로 닫음", "review_merged_by_ai": "AI가 같은 장학을 병합",
    "review_corrected_by_ai": "AI가 값을 고치고 닫음", "review_revised_by_ai": "AI가 정정·연장을 적용",
    "review_resolved": "검토 완료 처리", "review_override_applied": "검토에서 값 수정",
    "revision_applied": "정정·연장 적용", "identity_proposal_rejected": "병합 후보를 다른 장학으로 판정",
    "merged": "장학 병합", "merge_reverted": "병합 취소", "split_queued": "장학 분리 요청",
    "manual_override_set": "값 직접 수정", "manual_override_removed": "직접 수정 취소",
    "crawl_requested": "수동 수집 요청", "job_retried": "작업 재시도", "document_reprocess_queued": "재파싱 요청",
    "extraction_reprocess_queued": "AI 재추출 요청", "embedding_reprocess_queued": "검색 색인 다시 만들기 요청",
    "embedding_backfill_queued": "전체 재색인 요청", "embedding_recompute_queued": "임베딩 다시 계산 요청",
    "embedding_profile_activated": "검색 색인 전환", "setting_updated": "AI 설정 변경",
    "setting_override_removed": "AI 설정 기본값으로 되돌림", "secret_rotated": "API key 저장",
    "secret_override_removed": "저장한 API key 삭제(.env 사용)", "provider_connection_tested": "AI 연결 확인",
    "source_toggled": "수집 켜기/끄기", "source_raw_policy_updated": "원문 공개 정책 변경",
    "source_proxy_updated": "수집 proxy 저장", "source_proxy_removed": "수집 proxy 해제",
    "admin_user_created": "계정 생성", "admin_user_deleted": "계정 삭제", "admin_password_reset": "비밀번호 초기화",
    "admin_password_changed": "비밀번호 변경", "api_key_created": "API key 발급", "api_key_revoked": "API key 폐기",
}

# Exception class name (stored as error_code) -> (what happened, what to do).
ERROR_HELP: dict[str, tuple[str, str]] = {
    "CancelledError": ("작업 처리기가 멈추며 중단됨",
                       "worker가 재시작·배포되면서 멈춘 것입니다. 자동으로 다시 실행되므로 할 일은 없습니다."),
    "StaleLock": ("작업 처리기 응답 끊김",
                  "실행 중이던 worker가 15분 넘게 응답하지 않아 작업을 회수했습니다. 자동으로 다시 실행됩니다."),
    "APITimeoutError": ("AI 응답 시간 초과",
                        "AI 모델이 제한 시간 안에 답하지 않았습니다. 자동 재시도로 대개 해결되며, 반복되면 "
                        "AI 설정의 '요청 제한 시간'을 늘리세요."),
    "ReadTimeout": ("응답 시간 초과", "상대 서버가 제때 답하지 않았습니다. 잠시 뒤 재시도하세요."),
    "ConnectTimeout": ("연결 시간 초과", "상대 서버에 연결하지 못했습니다. 잠시 뒤 재시도하세요."),
    "TimeoutError": ("시간 초과", "처리가 제한 시간 안에 끝나지 않았습니다. 잠시 뒤 재시도하세요."),
    "APIConnectionError": ("AI 서비스 연결 실패",
                           "AI 서비스에 연결하지 못했습니다. AI 설정의 API 주소와 네트워크를 확인하세요."),
    "ConnectError": ("연결 실패", "상대 서버에 연결하지 못했습니다. 주소와 네트워크(proxy)를 확인하세요."),
    "RateLimitError": ("AI 요청 한도 초과", "AI 서비스의 요청 한도를 넘었습니다. 자동으로 다시 시도합니다."),
    "AuthenticationError": ("AI API key 거부", "AI 서비스가 API key를 받아들이지 않았습니다. AI 설정에서 API key를 확인하세요."),
    "PermissionDeniedError": ("AI 사용 권한 없음", "API key에 이 모델을 쓸 권한이 없습니다. AI 설정을 확인하세요."),
    "NotFoundError": ("모델·주소를 찾을 수 없음", "AI 설정의 모델 이름이나 API 주소가 맞는지 확인하세요."),
    "BadRequestError": ("AI 서비스가 요청을 거부", "입력이 너무 길거나 모델이 지원하지 않는 요청일 수 있습니다. "
                        "LLM 요청·응답 원본에서 오류 내용을 확인하세요."),
    "InternalServerError": ("AI 서비스 내부 오류", "AI 서비스 쪽 문제입니다. 잠시 뒤 재시도하세요."),
    "TransientProviderError": ("AI 서비스 일시 장애", "AI 서비스가 잠시 응답하지 않았습니다. 자동으로 다시 시도합니다."),
    "StructuredOutputError": ("AI 응답 형식 오류",
                              "AI 응답이 정해진 형식에 맞지 않았습니다. 재시도하거나 공고 화면에서 'AI 재추출'을 "
                              "요청하세요. 반복되면 공고 화면의 LLM 요청·응답 원본을 확인하세요."),
    "ValidationError": ("값 검증 실패", "AI 응답이나 입력값이 규칙에 맞지 않았습니다. 재시도하고, 반복되면 개발자에게 알리세요."),
    "CapabilityError": ("AI 설정 부족", "필요한 모델이나 API key가 설정되어 있지 않습니다. AI 설정을 확인하세요."),
    "LookupError": ("이전 단계 결과 없음", "필요한 파싱 결과나 AI 추출 결과를 찾지 못했습니다. 공고 화면에서 재파싱 후 다시 시도하세요."),
    "DataError": ("데이터 저장 실패", "값이 데이터베이스 형식에 맞지 않아 저장하지 못했습니다. 오류 내용을 개발자에게 전달하세요."),
    "IntegrityError": ("데이터 규칙 위반", "데이터베이스 규칙에 맞지 않아 저장하지 못했습니다. 오류 내용을 개발자에게 전달하세요."),
    "DBAPIError": ("데이터베이스 오류", "오류 내용을 개발자에게 전달하세요."),
    "HTTPStatusError": ("상대 서버 오류 응답", "원문 사이트나 AI 서비스가 오류로 답했습니다. 잠시 뒤 재시도하세요."),
    "UnicodeDecodeError": ("문자 인코딩 오류", "파일 글자 인코딩을 읽지 못했습니다. 원본 파일을 내려받아 확인하세요."),
    "TestJob": ("테스트 작업", "시험용으로 만든 작업입니다. 무시해도 됩니다."),
}
CODE_ERROR = ("처리 중 예기치 못한 오류",
              "파일 형식이 특이하거나 처리 코드의 문제일 수 있습니다. 원본을 확인하고, 재시도해도 반복되면 "
              "오류 내용을 개발자에게 전달하세요.")
for _name in ("IndexError", "KeyError", "ValueError", "TypeError", "AttributeError", "AssertionError",
              "RuntimeError", "ZeroDivisionError", "OSError", "Exception"):
    ERROR_HELP.setdefault(_name, CODE_ERROR)


def label(domain: str, value: Any) -> str:
    if value is None:
        return ""
    return LABELS.get(domain, {}).get(str(value), (str(value), ""))[0]


def tone(domain: str, value: Any) -> str:
    return LABELS.get(domain, {}).get(str(value), ("", ""))[1]


def chip(domain: str, value: Any, prefix: str = "") -> Markup:
    """A status chip in Korean; the stored value stays in the tooltip."""
    if value is None or value == "":
        return Markup('<span class="muted">—</span>')
    return Markup('<span class="chip {}" title="{}">{}{}</span>').format(
        tone(domain, value), value, prefix, label(domain, value))


def error_help(code: Any, message: Any = None) -> dict[str, str]:
    """What an error means for staff. ``code`` is usually an exception class name."""
    code = str(code or "")
    text = str(message or "")
    if not code:  # "IndexError: list index out of range", "공고 12 수집 실패 — ConnectError: ..."
        found = re.search(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Timeout|Lock))\b:", text[:300])
        if found:
            code = found.group(1)
    summary, action = ERROR_HELP.get(code, ("", ""))
    if not summary:
        lowered = text.lower()
        if "timeout" in lowered or "timed out" in lowered:
            summary, action = ERROR_HELP["ReadTimeout"]
        elif "rate limit" in lowered or "429" in lowered:
            summary, action = ERROR_HELP["RateLimitError"]
        elif code or text:
            summary, action = ("오류", "오류 내용을 확인하고 재시도하세요. 반복되면 개발자에게 전달하세요.")
    return {"code": code, "summary": summary, "action": action, "message": text}


ACTION_REFUSED_HELP = ("AI 2차 검토가 처리 방법을 정했지만 시스템 규칙(원문 인용 일치, 대상 장학 범위 등)에 "
                       "맞지 않아 직접 적용하지 않고 사람에게 넘겼습니다.")

# Segments of extraction field paths ("/opportunities/0/eligibility/regions/evidence/0").
FIELD_NAMES = {
    "name": "이름", "organization": "기관", "academic_year": "연도", "semester": "학기", "round_label": "회차",
    "application_windows": "신청 기간", "start": "시작", "end": "마감", "benefits": "지원 내용",
    "amount_min": "최소 금액", "amount_max": "금액", "tuition_percentage": "등록금 비율", "frequency": "지급 주기",
    "duration": "지원 기간", "eligibility": "지원 자격", "student_types": "대상", "grades": "학년", "majors": "전공",
    "gpa_min": "성적", "gpa_scale": "성적 만점", "income_bracket_max": "소득 구간", "regions": "지역",
    "schools": "학교", "enrollment_states": "학적", "residual_conditions": "그 밖의 조건",
    "selection": "선발", "method": "선발 방법", "selection_count": "선발 인원", "nomination_quota": "학교 추천 인원",
    "result_date": "결과 발표", "required_documents": "제출 서류", "application_methods": "신청 방법",
    "contacts": "연락처", "phone": "전화", "email": "이메일", "url": "URL", "address": "주소", "department": "부서",
    "title": "제목", "summary": "요약", "description": "설명", "status": "상태",
    "university_nomination_count": "학교 추천 인원", "final_selection_count": "최종 선발 인원",
    "recruitment_count": "모집 인원",
}


def field_label(path: str) -> str:
    """"/opportunities/0/required_documents/3/evidence/0" -> "제출 서류 4번째"."""
    # Also "application_windows[local_key=application].end.value" as AI proposals write it.
    plain = re.sub(r"\[[^\]]*\]", "", str(path))
    parts = [part for part in re.split(r"[/.]", plain.strip("/")) if part]
    if len(parts) >= 2 and parts[0] == "opportunities" and parts[1].isdigit():
        parts = parts[2:]
    words: list[str] = []
    for part in parts:
        if part in {"evidence", "value"}:
            break
        if part.isdigit():
            if words:
                words[-1] = f"{words[-1]} {int(part) + 1}번째"
            continue
        words.append(FIELD_NAMES.get(part, part))
    return " › ".join(words) or str(path)


QUALITY_FLAGS = {
    "field_conflict": "원문 안에서 값이 서로 다릅니다(근거 충돌)",
    "provenance_document_partial": "첨부 일부를 완전히 읽지 못했습니다",
    "asset_collection_incomplete": "첨부를 모두 내려받지 못했습니다",
    "ai_review_cleared": "AI 2차 검토에서 문제 없음으로 판단했습니다",
    "ai_corrected": "AI 2차 검토가 값을 고쳤습니다",
    "llm_transcription": "이미지를 AI로 옮겨 적었습니다",
    "scanned_pages": "스캔 페이지가 있습니다",
    "hwp5txt_recovered_lines": "누락된 줄을 보조 방법으로 복구했습니다",
    "application downgraded coverage because one or more documents were not parsed":
        "읽지 못한 문서가 있어 '일부 불완전'으로 낮췄습니다",
}


def quality_flag(flag: Any) -> str:
    """One quality flag or extraction warning in words staff can act on."""
    text = str(flag)
    if text in QUALITY_FLAGS:
        return QUALITY_FLAGS[text]
    match = re.match(r"evidence_validation_warning: (/\S+): (.*)$", text)
    if match:
        path, problem = match.groups()
        why = ("인용한 문장이 원문 블록에 없습니다" if "quote is absent" in problem
               else "가리킨 원문 블록이 없습니다" if "unknown block" in problem else problem)
        return f"근거 확인 필요 · {field_label(path)}: {why}"
    if text.startswith("crosscheck_disagreement"):
        return "교차 확인 모델과 추출 결과가 달랐습니다 (" + text.split(" ", 1)[-1] + ")"
    return text


# Result messages after an action, keyed by the ``done`` query parameter. {n} is a count.
DONE_MESSAGES = {
    "crawl_requested": "수집을 요청했습니다. 수집기가 곧 시작하며, 진행 상황은 이 목록에서 볼 수 있습니다.",
    "job_retried": "작업을 다시 대기열에 넣었습니다. 작업 처리기(worker)가 곧 실행합니다.",
    "source_toggled": "수집 켜기/끄기 설정을 바꿨습니다.",
    "source_raw_policy": "원문 공개 정책을 저장했습니다.",
    "source_proxy_saved": "수집 proxy를 저장했습니다.",
    "source_proxy_removed": "수집 proxy를 해제했습니다. 기본 설정(.env)이나 직접 연결을 씁니다.",
    "reparse_queued": "재파싱 작업을 작업 큐에 넣었습니다. 끝나면 AI 추출도 자동으로 다시 실행됩니다.",
    "extract_queued": "AI 재추출 작업을 작업 큐에 넣었습니다. 몇 분 뒤 이 화면을 새로고침해 결과를 확인하세요.",
    "embed_queued": "검색 색인 작업을 작업 큐에 넣었습니다. 끝나면 공개할 수 있습니다.",
    "backfill_queued": "임베딩 작업 {n}건을 작업 큐에 넣었습니다.",
    "embeddings_queued": "임베딩 작업 {n}건을 작업 큐에 넣었습니다. 모두 끝나면 '이 색인으로 전환'을 누르세요.",
    "profile_activated": "검색에 쓰는 색인을 전환했습니다.",
    "review_resolved": "검토 완료로 표시했습니다.",
    "review_override_applied": "값을 수정하고 검토를 완료했습니다.",
    "revision_applied": "정정·연장 내용으로 새 버전(공개 전)을 만들었습니다. 내용을 확인한 뒤 공개하세요.",
    "proposal_rejected": "다른 장학으로 판정했습니다. 두 장학은 따로 유지됩니다.",
    "published": "이 버전을 공개했습니다. 공개 API에 바로 반영됩니다.",
    "unpublished": "비공개로 전환했습니다. 공개 API에서 더 이상 보이지 않습니다.",
    "override_set": "값을 직접 수정했습니다. 공개 API에 수정한 값이 나갑니다.",
    "override_removed": "직접 수정을 취소했습니다. 추출된 원래 값으로 돌아갑니다.",
    "merged": "병합했습니다. 병합된 장학은 이 대표 장학으로 연결됩니다.",
    "unmerged": "병합을 취소했습니다. 이 장학은 다시 별도 장학입니다.",
    "split_queued": "분리 작업을 요청했습니다. AI 추출이 끝나면 새 장학이 생기고 검토함에서 확인할 수 있습니다.",
    "setting_saved": "설정을 저장했습니다. 다음 작업부터 적용됩니다.",
    "setting_reset": "관리자 설정을 지우고 .env 값(없으면 기본값)으로 되돌렸습니다.",
    "embedding_setting_saved": "설정을 저장했습니다. 새 설정으로 임베딩 작업 {n}건을 작업 큐에 넣었습니다.",
    "secret_saved": "API key를 저장했습니다. 화면에는 가린 값만 표시됩니다.",
    "secret_removed": "저장한 API key를 지웠습니다. 이제 .env의 key를 씁니다.",
    "test_done": "연결 확인을 마쳤습니다. 결과는 아래 '연결 확인'에 있습니다.",
    "key_revoked": "API key를 폐기했습니다. 이 key로 오는 요청은 바로 거부됩니다.",
}
# Problems reported back to a page instead of a separate error page, keyed by ``problem``.
PROBLEM_MESSAGES = {
    "crawl_active": "이 수집 대상은 이미 수집 중이거나 대기 중입니다. 끝난 뒤 다시 요청하세요.",
    "job_running": "실행 중인 작업은 재시도할 수 없습니다. 15분 넘게 응답이 없으면 자동으로 회수되어 다시 실행됩니다.",
    "not_embedded": "검색 색인이 아직 없어 공개하지 못했습니다. '검색 색인 다시 만들기'를 누르고 작업이 끝난 뒤 공개하세요.",
    "test_failed": "연결 확인에 실패했습니다. 아래 '연결 확인'에서 오류 내용을 보세요.",
    "profile_incomplete": "아직 임베딩이 끝나지 않은 장학이 {n}건 있어 전환하지 않았습니다. 작업이 끝난 뒤 다시 누르세요.",
    "profile_mismatch": "이 프로필은 지금 AI 설정의 임베딩 모델과 달라 재색인할 수 없습니다. AI 설정 화면의 '임베딩 다시 계산'을 쓰세요.",
    "review_closed": "이 검토 항목은 이미 처리되었습니다(다른 사람이 먼저 처리했거나 AI 2차 검토 중일 수 있습니다).",
    "master_key_missing": "서버에 MASTER_KEY가 설정되어 있지 않아 비밀 값을 저장할 수 없습니다. 운영 담당자에게 문의하세요.",
}


def message(kind: str, key: str | None, n: str | None = None) -> str | None:
    table = DONE_MESSAGES if kind == "done" else PROBLEM_MESSAGES
    template = table.get(key or "")
    if template is None:
        return None
    count = n if n and n.isdigit() else "0"
    return template.replace("{n}", count)


def with_message(path: str, key: str, *, problem: bool = False, n: int | None = None) -> str:
    """``path`` with a result message attached, keeping its query and #fragment."""
    parts = urlsplit(path)
    query = [(name, value) for name, value in parse_qsl(parts.query) if name not in {"done", "problem", "n"}]
    query.append(("problem" if problem else "done", key))
    if n is not None:
        query.append(("n", str(n)))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


# Korean for the English details raised as HTTPException in admin code paths.
ERROR_PAGE_TITLES = {
    400: "요청을 처리할 수 없습니다", 401: "로그인이 필요합니다", 403: "권한이 없습니다",
    404: "찾을 수 없습니다", 409: "지금 상태에서는 할 수 없습니다", 415: "브라우저에서 열 수 없는 형식입니다",
    422: "입력값을 확인하세요", 429: "요청이 너무 많습니다",
}


def register(env: Any) -> None:
    env.filters["quality_flag"] = quality_flag
    env.globals.update(label=label, tone=tone, chip=chip, error_help=error_help, field_label=field_label,
                       quality_flag=quality_flag, audit_action=lambda value: AUDIT_ACTIONS.get(value, value),
                       actor_name=lambda value: ACTOR_NAMES.get(value, value), flash_message=message,
                       ACTION_REFUSED_HELP=ACTION_REFUSED_HELP)
