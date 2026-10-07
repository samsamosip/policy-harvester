"""필드별 현재값 선택의 보수적인 참고 구현. 네트워크/DB/LLM을 호출하지 않는다.

중요한 경계:
1. 같은 게시글/문서 슬롯의 과거 스냅샷은 이미 이력으로 분리한 뒤 호출한다.
   같은 게시글의 본문 수정에는 [수정] 표기가 없어도 새 원문 버전을 만든다.
2. 입력 후보는 같은 모집회차, 같은 필드, 같은 접수 단계의 *현재* 근거다.
   재단 접수와 대학 추천 마감은 서로 다른 FieldScope로 처리한다.
3. VerifiedAmendment의 검증 상태는 신뢰할 수 있는 애플리케이션/검토자가 만든다.
   LLM 출력의 boolean이나 confidence를 그대로 복사해서는 안 된다.
4. 여기서는 후보 ID별 명시적 supersession 관계만 계산한다. 원문 근거의 의미,
   소스 권한, 날짜 정규화, 사업 동일성, 병합/분리는 별도 서비스에서 검증한다.
5. 실행 결과는 설계 예시다. 실제 사이트 전체를 구조화한 결과가 아니다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

RULE_VERSION = "field-resolution-1.0"


@dataclass(frozen=True)
class FieldScope:
    opportunity_id: str
    cycle_key: str
    field_path: str
    window_key: str | None


@dataclass(frozen=True)
class Candidate:
    key: str
    scope: FieldScope
    value: Any  # 사전에 정규화한 JSON 값. 원문 문자열의 정렬로 날짜를 추정하지 않는다.
    source_notice_id: str
    source_version_id: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedAmendment:
    key: str
    scope: FieldScope
    kind: Literal["extension", "correction", "cancellation", "reopened", "replacement"]
    selected_candidate_key: str
    supersedes_candidate_keys: tuple[str, ...]
    directive_evidence_ids: tuple[str, ...]
    same_cycle_verified: bool
    field_scope_verified: bool
    intent_and_new_value_verified: bool
    precedence_verified: bool
    # precedence_verified에는 다른 정정과의 순서 및 대상 근거를 포함한다.
    # 수집 시각/게시글 번호가 크다는 사실만으로 이를 true로 만들지 않는다.


@dataclass(frozen=True)
class VerifiedPendingAmendment:
    """변경 의도/범위는 확인했으나 새 값은 아직 확인하지 못한 미해결 변경.

    관련 없는 표식이나 LLM의 미검증 제안을 그대로 넘기지 않는다. 새 값을
    확인해 해결한 뒤에는 호출자가 이 pending 항목을 해소하고 다시 계산한다.
    """
    key: str
    scope: FieldScope
    reason: str
    directive_evidence_ids: tuple[str, ...]
    same_cycle_verified: bool
    field_scope_verified: bool
    change_intent_verified: bool


@dataclass(frozen=True)
class Resolution:
    status: Literal["agreed", "resolved_explicit_update", "unresolved", "not_stated"]
    selected_candidate_key: str | None
    selected_value: Any | None
    all_candidate_keys: tuple[str, ...]
    superseded_candidate_keys: tuple[str, ...]
    applied_directive_keys: tuple[str, ...]
    reasons: tuple[str, ...]
    rule_version: str = RULE_VERSION


def _value_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def resolve_field(
    candidates: list[Candidate],
    directives: list[VerifiedAmendment],
    pending_amendments: list[VerifiedPendingAmendment] | None = None,
) -> Resolution:
    """단일 필드/단계의 후보를 계산하며 모든 원래 후보 ID를 반환한다.

    최신 게시일 우선, 본문 무조건 우선, LLM confidence 우선 규칙은 없다.
    여러 정정이 서로 다른 최종 값을 주장하거나 정정 관계가 순환하면 unresolved다.
    """
    verified_pending = [item for item in (pending_amendments or []) if (
        item.same_cycle_verified and item.field_scope_verified
        and item.change_intent_verified and item.directive_evidence_ids
    )]
    if not candidates and verified_pending:
        if len({item.scope for item in verified_pending}) != 1:
            raise ValueError("separate pending amendment field/stage scopes")
        return Resolution("unresolved", None, None, (), (), (), tuple(
            f"pending amendment {item.key}: {item.reason}" for item in verified_pending
        ))
    if not candidates:
        return Resolution("not_stated", None, None, (), (), (), ("no stated candidate",))

    by_key = {candidate.key: candidate for candidate in candidates}
    if len(by_key) != len(candidates):
        raise ValueError("candidate keys must be unique")
    scope = candidates[0].scope
    if any(candidate.scope != scope for candidate in candidates):
        raise ValueError("separate opportunity/cycle/field/stage scopes before resolving")
    if any(candidate.value is None or not candidate.evidence_ids for candidate in candidates):
        raise ValueError("only stated, evidenced values are candidates")
    if len({directive.key for directive in directives}) != len(directives):
        raise ValueError("directive keys must be unique")

    blocking = [item for item in verified_pending if item.scope == scope]
    if blocking:
        return Resolution("unresolved", None, None, tuple(sorted(by_key)), (), (), tuple(
            f"pending amendment {item.key}: {item.reason}; old value is historical/unconfirmed"
            for item in blocking
        ))

    canonical_values = {key: _value_key(candidate.value) for key, candidate in by_key.items()}
    edges: dict[str, set[str]] = {key: set() for key in by_key}
    applied: list[str] = []
    notes: list[str] = []
    for directive in directives:
        checks = (
            directive.same_cycle_verified,
            directive.field_scope_verified,
            directive.intent_and_new_value_verified,
            directive.precedence_verified,
        )
        referenced = (directive.selected_candidate_key, *directive.supersedes_candidate_keys)
        valid = (
            directive.scope == scope
            and all(checks)
            and bool(directive.directive_evidence_ids)
            and bool(directive.supersedes_candidate_keys)
            and all(key in by_key for key in referenced)
            and directive.selected_candidate_key not in directive.supersedes_candidate_keys
        )
        if not valid:
            notes.append(f"ignored incomplete or wrong-scope directive: {directive.key}")
            continue
        for old_key in directive.supersedes_candidate_keys:
            edges[old_key].add(directive.selected_candidate_key)
        applied.append(directive.key)

    # Kahn 위상 정렬로 순환 검출. A→B→A는 별도 후보 A2로 기록해야 한다.
    indegree = {key: 0 for key in by_key}
    for successors in edges.values():
        for successor in successors:
            indegree[successor] += 1
    ready = sorted(key for key, degree in indegree.items() if degree == 0)
    visited_count = 0
    while ready:
        key = ready.pop()
        visited_count += 1
        for successor in sorted(edges[key]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                ready.append(successor)

    all_keys = tuple(sorted(by_key))
    if visited_count != len(by_key):
        return Resolution("unresolved", None, None, all_keys, (), tuple(applied),
                          tuple(notes + ["cyclic supersession; do not publish a selected value"]))

    remaining = sorted(key for key, successors in edges.items() if not successors)
    superseded = tuple(sorted(key for key, successors in edges.items() if successors))
    if len({canonical_values[key] for key in remaining}) != 1:
        return Resolution("unresolved", None, None, all_keys, superseded, tuple(applied),
                          tuple(notes + ["multiple current values remain"]))

    # 여러 근거가 같은 값이면 대표 ID만 선택한다. 모든 후보는 그대로 남긴다.
    selected = remaining[0]
    status = "resolved_explicit_update" if superseded else "agreed"
    return Resolution(status, selected, by_key[selected].value, all_keys, superseded,
                      tuple(applied), tuple(notes + ["one current value remains"]))


def demonstration() -> dict[str, Any]:
    """실제 관찰 날짜를 사용한 설계 예시. example: ID는 실제 DB/추출 실행 ID가 아니다."""
    daejeon = FieldScope("example:daejeon-2026-achievement", "2026", "/end", "student_online_application")
    old = Candidate("old-sep23", daejeon, "2026-09-23T17:00:00+09:00", "45364",
                    "example:45364-v1", ("example:45364:deadline",))
    new = Candidate("extended-oct08", daejeon, "2026-10-08T17:00:00+09:00", "45671",
                    "example:45671-v1", ("example:45671:deadline",))
    extension = VerifiedAmendment(
        "example:verified-extension", daejeon, "extension", new.key, (old.key,),
        ("example:45671:title-period-extension", "example:45671:deadline-period-extension"),
        True, True, True, True,
    )

    hy = FieldScope("example:hy-2026", "2026", "/end", "foundation_online_application")
    body = Candidate("body-oct08", hy, "2026-10-08T17:00:00+09:00", "45691",
                     "example:45691-v1", ("example:45691:body:deadline",))
    pdf = Candidate("pdf-sep30", hy, "2026-09-30T17:00:00+09:00", "45691",
                    "example:45691-v1", ("example:41441:page2:deadline",))
    incomplete = VerifiedAmendment(
        "example:title-marker-without-new-value-verification", daejeon, "extension", new.key,
        (old.key,), ("example:title-marker-only",), True, True, False, False,
    )
    pending = VerifiedPendingAmendment(
        "example:confirmed-extension-unreadable-date", daejeon,
        "extension of this window confirmed, replacement deadline attachment unreadable",
        ("example:explicit-extension-of-this-window",), True, True, True,
    )
    return {
        "notice": "Illustrative decisions; candidate/evidence IDs are examples, not executed extraction output.",
        "daejeon_explicit_extension": asdict(resolve_field([old, new], [extension])),
        "hy_unexplained_conflict": asdict(resolve_field([body, pdf], [])),
        "title_marker_alone": asdict(resolve_field([old, new], [incomplete])),
        "confirmed_extension_date_missing": asdict(resolve_field([old], [], [pending])),
    }


if __name__ == "__main__":
    print(json.dumps(demonstration(), ensure_ascii=False, indent=2))
