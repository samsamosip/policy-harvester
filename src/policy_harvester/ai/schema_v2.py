"""Extraction schema v2 (``scholarship_v2``).

Compared with v1 it leaves to the program what the program can compute or already knows:
evidence offsets and confidence (offsets come from the quote), timezone (Asia/Seoul), date
precision and raw text (from the date shape and the quote), currency (KRW), the opportunity
status (from the windows and today), the eligibility original text (from the cited blocks),
identity candidates and referenced notice ids (decided by the program, unknown to the model).
Values that were free text but drive filters are enums. Duplicates are merged: the result
date is a ``result`` window, one headcount pair replaces three overlapping counts, the window
channel is the application method, and first-come/rolling is one closing rule.
"""
from __future__ import annotations

import re
from datetime import date, time, timedelta
from typing import Any, Generic, Literal, TypeVar

from pydantic import Field, HttpUrl, JsonValue, model_validator

from .schema import MissingState, StrictModel, interpret_placeholder_counts, normalize_fact_states

T = TypeVar("T")

SCHEMA_VERSION = "scholarship_v2"


class EvidenceRef(StrictModel):
    block_id: str
    quote: str
    evidence_type: Literal["explicit", "explicit_correction", "derived", "inferred"]


class Fact(StrictModel, Generic[T]):
    value: T | None
    state: MissingState
    evidence: list[EvidenceRef]

    @model_validator(mode="after")
    def validate_value_state(self):
        if self.state in {"stated", "resolved_update"} and self.value is None:
            raise ValueError("a resolved fact requires a value")
        if self.state not in {"stated", "resolved_update"} and self.value is not None:
            raise ValueError("an unresolved fact cannot carry a value")
        if self.state in {"stated", "explicitly_none", "resolved_update"} and not self.evidence:
            raise ValueError("stated facts require evidence")
        return self


class DateValue(StrictModel):
    """A local (Asia/Seoul) date: a full date, or only a month when the source gives no day."""
    date: date | None
    month: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    time: time | None

    @model_validator(mode="before")
    @classmethod
    def normalize_end_of_day(cls, data: Any) -> Any:
        # "D 24:00" is "D+1 00:00"; the original wording stays in the evidence quote.
        if isinstance(data, dict) and str(data.get("time") or "") in {"24:00", "24:00:00"}:
            try:
                day = date.fromisoformat(str(data.get("date")))
            except ValueError:
                return data
            return {**data, "date": (day + timedelta(days=1)).isoformat(), "time": "00:00:00"}
        return data

    @model_validator(mode="after")
    def validate_shape(self):
        if self.date is None and self.month is None:
            raise ValueError("a date needs a day or a month")
        if self.date is not None and self.month is not None:
            raise ValueError("give either a full date or a month, not both")
        if self.time is not None and self.date is None:
            raise ValueError("time requires a full date")
        return self

    @property
    def precision(self) -> str:
        return "minute" if self.time else ("date" if self.date else "month")


class ApplicationWindow(StrictModel):
    local_key: str
    stage: Literal["application", "additional_application", "document_delivery", "nomination", "result", "other"]
    label: str | None
    submit_to: Literal["university", "provider", "other", "unknown"]
    start: Fact[DateValue]
    end: Fact[DateValue]
    closing_rule: Literal["fixed", "first_come", "rolling", "unknown"]


class Benefit(StrictModel):
    benefit_type: Literal["cash", "tuition", "living_cost", "housing", "in_kind", "service", "loan", "other"]
    amount_min: Fact[int]
    amount_max: Fact[int]
    tuition_percentage: Fact[float]
    frequency: Literal["once", "per_semester", "per_year", "monthly", "other", "unknown"]
    duration: str | None
    description: str

    @model_validator(mode="after")
    def validate_amounts(self):
        if self.amount_min.value is not None and self.amount_max.value is not None:
            if self.amount_min.value > self.amount_max.value:
                raise ValueError("minimum benefit exceeds maximum")
        return self


class EligibilityPredicate(StrictModel):
    field: Literal[
        "student_type", "grade", "major", "gpa", "income_bracket", "age", "residency",
        "school", "enrollment_status", "military", "employment", "other",
    ]
    operator: Literal["eq", "neq", "in", "not_in", "gte", "gt", "lte", "lt", "exists", "not_exists"]
    value: JsonValue
    evidence: list[EvidenceRef]


class EligibilityNode(StrictModel):
    operator: Literal["and", "or", "not", "predicate"]
    predicate: EligibilityPredicate | None
    children: list["EligibilityNode"]

    @model_validator(mode="after")
    def validate_tree(self):
        if self.operator == "predicate":
            if self.predicate is None or self.children:
                raise ValueError("predicate node is malformed")
        elif self.predicate is not None or not self.children:
            raise ValueError("logical node is malformed")
        if self.operator == "not" and len(self.children) != 1:
            raise ValueError("not requires one child")
        return self


StudentType = Literal["elementary", "middle", "high", "undergraduate", "graduate", "other"]
EnrollmentState = Literal["enrolled", "on_leave", "returning", "incoming", "completed", "other"]


class Eligibility(StrictModel):
    student_types: Fact[list[StudentType]]
    grades: Fact[list[int]]
    majors: Fact[list[str]]
    gpa_min: Fact[float]
    gpa_scale: Fact[float]
    income_bracket_max: Fact[int]
    regions: Fact[list[str]]
    schools: Fact[list[str]]
    enrollment_states: Fact[list[EnrollmentState]]
    rule_tree: EligibilityNode | None
    residual_conditions: list[Fact[str]]


class Selection(StrictModel):
    selection_count: Fact[int]
    nomination_quota: Fact[int]
    count_text: str | None
    method: Fact[str]


class ApplicationMethod(StrictModel):
    method: Literal["online", "email", "postal", "visit", "university_nomination", "other"]
    url: Fact[HttpUrl]
    email: Fact[str]
    address: Fact[str]
    instructions: str


class RequiredDocument(StrictModel):
    name: str
    requiredness: Literal["required", "conditional", "optional", "unknown"]
    condition: str | None
    evidence: list[EvidenceRef]


class Contact(StrictModel):
    organization: Fact[str]
    department: Fact[str]
    person: Fact[str]
    phone: Fact[str]
    email: Fact[str]


Semester = Literal["spring", "fall", "summer", "winter", "first_half", "second_half", "annual", "other"]


class Opportunity(StrictModel):
    local_key: str
    name: Fact[str]
    aliases: list[str]
    organization: Fact[str]
    category: Literal["scholarship", "loan", "work_study", "living_support", "program", "other"]
    academic_year: Fact[int]
    semester: Fact[Semester]
    round_label: Fact[str]
    summary: str
    benefits: list[Benefit]
    application_windows: list[ApplicationWindow]
    eligibility: Eligibility
    selection: Selection
    application_methods: list[ApplicationMethod]
    required_documents: list[RequiredDocument]
    contacts: list[Contact]


class FieldPatch(StrictModel):
    field_path: str
    scope_key: str
    previous_value: JsonValue
    replacement_value: JsonValue
    evidence: list[EvidenceRef]

    @model_validator(mode="after")
    def validate_path(self):
        if not self.field_path.startswith("/"):
            raise ValueError("field path must be a JSON pointer")
        return self


class Revision(StrictModel):
    kind: Literal["extension", "correction", "cancellation", "reopened", "repost", "additional_recruitment", "unknown"]
    opportunity_local_key: str | None
    same_cycle_evidence: list[EvidenceRef]
    marker_evidence: list[EvidenceRef]
    patches: list[FieldPatch]
    confidence: float = Field(ge=0, le=1)


COUNT_FIELDS = ("selection_count", "nomination_quota")


class ExtractionBundleV2(StrictModel):
    schema_version: Literal["scholarship_v2"]
    opportunities: list[Opportunity]
    revisions: list[Revision]
    coverage: Literal["complete", "partial", "failed"]
    warnings: list[str]

    @classmethod
    def normalize_payload(cls, data: Any) -> tuple[Any, list[str]]:
        data, notes = normalize_fact_states(data)
        if isinstance(data, dict):
            for item in data.get("opportunities") or []:
                if isinstance(item, dict) and "selection" in item:
                    item["selection"] = interpret_placeholder_counts(item["selection"], COUNT_FIELDS)
        return data, notes


def short_block_ids(blocks: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Map block UUIDs to short ids ("b1", "b2", ...) for the prompt, and back.

    Long UUIDs cost input tokens and models truncate or mistype them in evidence.
    """
    forward = {block_id: f"b{index}" for index, block_id in enumerate(blocks, start=1)}
    return forward, {short: block_id for block_id, short in forward.items()}


def restore_block_ids(value: Any, backward: dict[str, str]) -> Any:
    """Rewrite every evidence block_id from its short id to the block UUID (unknown ids kept)."""
    if isinstance(value, dict):
        restored = {key: restore_block_ids(child, backward) for key, child in value.items()}
        if isinstance(restored.get("block_id"), str):
            restored["block_id"] = backward.get(restored["block_id"], restored["block_id"])
        return restored
    if isinstance(value, list):
        return [restore_block_ids(child, backward) for child in value]
    return value


def quote_offsets(quote: str, block_text: str) -> tuple[int, int] | None:
    """Character span of a quote in its block, matching runs of whitespace loosely."""
    words = quote.split()
    if not words:
        return None
    match = re.search(r"\s+".join(re.escape(word) for word in words), block_text)
    return (match.start(), match.end()) if match else None


TITLE_REVISION_MARKERS = (
    (re.compile(r"(?:신청)?\s*기간\s*연장|\[연장\]|\(연장\)"), "extension"),
    (re.compile(r"\[(?:정정|수정|변경)\]|\((?:정정|수정|변경)\)"), "correction"),
    (re.compile(r"\[취소\]|\(취소\)|모집\s*취소"), "cancellation"),
    (re.compile(r"재공고|재게시"), "repost"),
    (re.compile(r"추가\s*모집"), "additional_recruitment"),
)


def with_title_revision(bundle: ExtractionBundleV2, title_block_id: str, title: str) -> ExtractionBundleV2:
    """A revision marker in the board title is mechanical; flag it even if the model did not.

    The candidate carries only the marker as evidence (no patches), so it goes to review where a
    person states what changed; it never changes a published value by itself.
    """
    if bundle.revisions:
        return bundle
    for pattern, kind in TITLE_REVISION_MARKERS:
        match = pattern.search(title)
        if match:
            single = bundle.opportunities[0].local_key if len(bundle.opportunities) == 1 else None
            revision = Revision(kind=kind, opportunity_local_key=single, same_cycle_evidence=[],
                                marker_evidence=[EvidenceRef(block_id=title_block_id, quote=match.group(0),
                                                             evidence_type="explicit")],
                                patches=[], confidence=0.5)
            return bundle.model_copy(update={"revisions": [revision]})
    return bundle
