from __future__ import annotations

import re

from datetime import date, time, timedelta
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator

T = TypeVar("T")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceRef(StrictModel):
    block_id: str
    quote: str
    char_start: int | None = Field(ge=0)
    char_end: int | None = Field(ge=0)
    evidence_type: Literal["explicit", "explicit_correction", "derived", "inferred"]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_range(self):
        if (self.char_start is None) != (self.char_end is None):
            raise ValueError("evidence offsets must be both present or absent")
        if self.char_start is not None and self.char_end < self.char_start:
            raise ValueError("evidence offset is inverted")
        return self


MissingState = Literal[
    "stated", "explicitly_none", "not_found", "unknown", "parsing_failed", "conflict",
    "resolved_update",
]


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


class LocalDateTime(StrictModel):
    date: date | None
    time: time | None
    timezone: str
    precision: Literal["year", "month", "date", "minute", "second", "range_text", "unknown"]
    raw_text: str

    @model_validator(mode="before")
    @classmethod
    def normalize_end_of_day(cls, data: Any) -> Any:
        # "D 24:00" is the same instant as "D+1 00:00" (ISO 8601); raw_text keeps the original.
        if isinstance(data, dict) and str(data.get("time") or "") in {"24:00", "24:00:00"}:
            try:
                day = date.fromisoformat(str(data.get("date")))
            except ValueError:
                return data
            return {**data, "date": (day + timedelta(days=1)).isoformat(), "time": "00:00:00"}
        return data

    @model_validator(mode="after")
    def validate_precision(self):
        if self.time is not None and self.date is None:
            raise ValueError("time requires a date")
        if self.time is not None and self.precision not in {"minute", "second"}:
            raise ValueError("time precision is inconsistent")
        return self


class ApplicationWindowDraft(StrictModel):
    local_key: str
    stage: Literal["application", "additional_application", "document_delivery", "nomination", "result", "other"]
    label: str | None
    start: Fact[LocalDateTime]
    end: Fact[LocalDateTime]
    application_authority: str | None
    channel: str | None
    first_come: Fact[bool]
    rolling: Fact[bool]


class BenefitDraft(StrictModel):
    benefit_type: Literal["cash", "tuition", "living_cost", "housing", "in_kind", "service", "loan", "other"]
    amount_min: Fact[int]
    amount_max: Fact[int]
    currency: str | None
    tuition_percentage: Fact[float]
    frequency: Fact[str]
    duration: Fact[str]
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
    source_text: str
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


class EligibilityDraft(StrictModel):
    student_types: Fact[list[str]]
    grades: Fact[list[int]]
    majors: Fact[list[str]]
    gpa_min: Fact[float]
    gpa_scale: Fact[float]
    income_bracket_max: Fact[int]
    regions: Fact[list[str]]
    schools: Fact[list[str]]
    enrollment_states: Fact[list[str]]
    rule_tree: EligibilityNode | None
    residual_conditions: list[Fact[str]]
    original_text: str


class ApplicationMethodDraft(StrictModel):
    method: Literal["online", "email", "postal", "visit", "university_nomination", "other"]
    url: Fact[HttpUrl]
    email: Fact[str]
    address: Fact[str]
    instructions: str


class RequiredDocumentDraft(StrictModel):
    name: str
    requiredness: Literal["required", "conditional", "optional", "unknown"]
    condition: str | None
    evidence: list[EvidenceRef]


class ContactDraft(StrictModel):
    organization: Fact[str]
    department: Fact[str]
    person: Fact[str]
    phone: Fact[str]
    email: Fact[str]


class SelectionDraft(StrictModel):
    university_nomination_count: Fact[int]
    final_selection_count: Fact[int]
    recruitment_count: Fact[int]
    method: Fact[str]
    result_date: Fact[LocalDateTime]
    # Headcounts written as a placeholder or an approximation ("00명" = a two-digit number,
    # "약 50명", "예산 범위 내") are not exact; the count facts stay unresolved and the
    # wording and its meaning are kept here, e.g. "두 자리 수 (원문: 00명)".
    count_text: str | None = None


class OpportunityDraft(StrictModel):
    local_key: str
    name: Fact[str]
    aliases: list[str]
    organization: Fact[str]
    category: Fact[str]
    academic_year: Fact[int]
    semester: Fact[str]
    round_label: Fact[str]
    status: Fact[str]
    summary: str
    benefits: list[BenefitDraft]
    application_windows: list[ApplicationWindowDraft]
    eligibility: EligibilityDraft
    selection: SelectionDraft
    application_methods: list[ApplicationMethodDraft]
    required_documents: list[RequiredDocumentDraft]
    contacts: list[ContactDraft]


class FieldPatchDraft(StrictModel):
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


class RevisionDraft(StrictModel):
    kind: Literal["extension", "correction", "cancellation", "reopened", "repost", "additional_recruitment", "unknown"]
    opportunity_local_key: str | None
    referenced_notice_ids: list[str]
    same_cycle_evidence: list[EvidenceRef]
    marker_evidence: list[EvidenceRef]
    patches: list[FieldPatchDraft]
    confidence: float = Field(ge=0, le=1)


class IdentityCandidateDraft(StrictModel):
    opportunity_local_key: str
    referenced_notice_id: str | None
    normalized_name: str
    organization: str | None
    academic_year: int | None
    semester: str | None
    candidate_kind: Literal["same_cycle", "new_cycle", "uncertain"]
    evidence: list[EvidenceRef]


UNRESOLVED_STATES = {"explicitly_none", "not_found", "unknown", "parsing_failed", "conflict"}
PLAIN_STRING_FIELDS = {"currency", "description", "summary", "instructions", "label", "condition",
                       "application_authority", "channel", "local_key", "original_text", "source_text"}
ALL_STATES = UNRESOLVED_STATES | {"stated", "resolved_update"}


def normalize_fact_states(data: Any) -> tuple[Any, list[str]]:
    """Conservative, deterministic fixes for recurring model slips before validation.

    A state the schema does not know (typically "inferred", confused with evidence_type) becomes
    "unknown" and loses its value: an inference is not a stated fact. An unresolved state that
    still carries a value loses the value. Nothing is ever upgraded to "stated".
    """
    notes: list[str] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            value = {key: walk(child, f"{path}/{key}") for key, child in value.items()}
            if {"value", "state", "evidence"} <= set(value):
                if path.rsplit("/", 1)[-1] in PLAIN_STRING_FIELDS:
                    # A plain string field written as a fact: keep only a stated value.
                    notes.append(f"{path}: unwrapped fact into plain value")
                    return value["value"] if value["state"] in {"stated", "resolved_update"} else None
                if value["state"] not in ALL_STATES:
                    notes.append(f"{path}: state {value['state']!r} -> 'unknown'")
                    value = {**value, "state": "unknown", "value": None}
                elif value["state"] in UNRESOLVED_STATES and value["value"] is not None:
                    notes.append(f"{path}: dropped value of unresolved state {value['state']!r}")
                    value = {**value, "value": None}
                if value["state"] in {"stated", "explicitly_none", "resolved_update"} and not value["evidence"]:
                    # Without a quote the value cannot be verified; it is not a stated fact.
                    notes.append(f"{path}: {value['state']!r} without evidence -> 'unknown'")
                    value = {**value, "state": "unknown", "value": None}
            field_path = value.get("field_path")
            if isinstance(field_path, str) and field_path and not field_path.startswith("/"):
                notes.append(f"{path}: field_path made a JSON pointer")
                value = {**value, "field_path": "/" + field_path.lstrip("./")}
            return value
        if isinstance(value, list):
            return [walk(child, f"{path}/{index}") for index, child in enumerate(value)]
        return value

    return walk(data, ""), notes


COUNT_FIELDS = ("university_nomination_count", "final_selection_count", "recruitment_count")
PLACEHOLDER_COUNT = re.compile(r"(?<![0-9])(0{1,3})\s*명")
DIGITS = {1: "한 자리 수", 2: "두 자리 수", 3: "세 자리 수"}


def interpret_placeholder_counts(selection: Any, count_fields: tuple[str, ...] = COUNT_FIELDS) -> Any:
    """"00명" masks a headcount (two digits), it is not an unreadable value.

    An unresolved count whose quote is such a placeholder becomes "unknown", and the wording with
    its meaning goes to ``count_text`` unless the model already wrote one.
    """
    if not isinstance(selection, dict):
        return selection
    selection = dict(selection)
    for key in count_fields:
        fact = selection.get(key)
        if not isinstance(fact, dict) or fact.get("state") in {"stated", "resolved_update"}:
            continue
        for reference in fact.get("evidence") or []:
            match = PLACEHOLDER_COUNT.search(str(reference.get("quote", "")))
            if match:
                selection[key] = {**fact, "state": "unknown", "value": None}
                if not selection.get("count_text"):
                    selection["count_text"] = f"{DIGITS[len(match.group(1))]} (원문: {match.group(0)})"
                break
    return selection


class ExtractionBundle(StrictModel):
    schema_version: Literal["scholarship_v1"]
    opportunities: list[OpportunityDraft]
    revisions: list[RevisionDraft]
    identity_candidates: list[IdentityCandidateDraft]
    coverage: Literal["complete", "partial", "failed"]
    warnings: list[str]

    @classmethod
    def normalize_payload(cls, data: Any) -> tuple[Any, list[str]]:
        data, notes = normalize_fact_states(data)
        if isinstance(data, dict):
            for item in data.get("opportunities") or []:
                if isinstance(item, dict) and "selection" in item:
                    item["selection"] = interpret_placeholder_counts(item["selection"])
        return data, notes


def _unspaced(value: str) -> str:
    # Korean spacing around particles and brackets varies between the source and a copied quote
    # ("( 혹은 고교성적 )" vs "(혹은 고교성적)"); the characters themselves must still match.
    return re.sub(r"\s+", "", value)


def validate_evidence(bundle: ExtractionBundle, blocks: dict[str, str]) -> list[str]:
    errors: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            if "block_id" in value and "quote" in value:
                block = blocks.get(value["block_id"])
                if block is None:
                    errors.append(f"{path}: unknown block {value['block_id']}")
                elif _unspaced(value["quote"]) not in _unspaced(block):
                    errors.append(f"{path}: quote is absent from block")
            for key, child in value.items():
                walk(child, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}/{index}")

    walk(bundle.model_dump(mode="json"))
    keys = [item.local_key for item in bundle.opportunities]
    if len(keys) != len(set(keys)):
        errors.append("opportunity local keys must be unique")
    return errors
