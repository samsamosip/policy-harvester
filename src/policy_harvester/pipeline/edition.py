"""Name/edition comparison shared by same-notice matching and cross-notice candidates.

A model may word the same scholarship differently ("2027년 의생명과학분야 대학 장학생 선발" vs
"2027년 아산사회복지재단 의생명과학분야 대학 장학생 선발"), while two different editions can
share a name ("2026년 X장학금" vs "2027년 X장학금", "1차" vs "2차"). Names are therefore split
into a core name and edition tokens, and every comparison returns one of:

* ``same``: core names agree, no edition token or date conflicts, and at least one edition
  anchor (year or application window) actually matches.
* ``different_edition``: core names agree but a known year/term/round/cohort or the
  application deadline disagrees. Never the same opportunity; it may share ``program_key``.
* ``different``: core names do not agree.
* ``uncertain``: everything else, e.g. a round marker on only one side ("X" vs "X 추가모집").

The tokens are only used for identity comparison; they are never stored as extracted facts.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable

RULE_VERSION = "edition-compare-1.0"
SAME_NAME = 0.82
RELATED_NAME = 0.55
DEADLINE_GAP_DAYS = 150

_BRACKETS = re.compile(r"[\[\(【<＜〈][^\]\)】>＞〉]*[\]\)】>＞〉]")
_YEAR = re.compile(r"(?<!\d)(20\d{2})(?!\d)|(?<![\d'’])['’]?(\d{2})\s*(?=년|학년도|-[12]\s*학기)")
_SEMESTER = re.compile(r"(?<!\d)([12])\s*학기")
_TERMS = (("하계", "summer"), ("여름", "summer"), ("동계", "winter"), ("겨울", "winter"),
          ("봄학기", "spring"), ("가을학기", "fall"))
_HALVES = (("상반기", "h1"), ("하반기", "h2"))
_ROUND = re.compile(r"(?<!\d)(\d{1,2})\s*차(?!\s*년)")
_COHORT = re.compile(r"(?<!\d)(\d{1,3})\s*기(?![가-힣])")
_EXTRA_ROUNDS = (("추가모집", "additional"), ("추가선발", "additional"), ("추가접수", "additional"),
                 ("재모집", "re"), ("재선발", "re"))
_REVISION = re.compile(r"(?:기간\s*)?(?:연장|정정|수정|변경|재공고)")
_FILLER = ("선발안내", "모집안내", "신청안내", "선발공고", "모집공고", "안내", "공고", "선발",
           "모집", "신청", "접수", "계획", "알림", "시행", "대상자", "학년도", "학기", "년도", "년")


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").lower()


@dataclass(frozen=True)
class EditionTokens:
    years: frozenset[int] = frozenset()
    terms: frozenset[str] = frozenset()
    halves: frozenset[str] = frozenset()
    rounds: frozenset[str] = frozenset()
    cohorts: frozenset[str] = frozenset()


def edition_tokens(name: str) -> EditionTokens:
    text = _nfkc(name)
    years = set()
    for full, short in _YEAR.findall(text):
        years.add(int(full) if full else 2000 + int(short))
    terms = {"spring" if value == "1" else "fall" for value in _SEMESTER.findall(text)}
    terms |= {term for needle, term in _TERMS if needle in text}
    halves = {half for needle, half in _HALVES if needle in text}
    rounds = set(_ROUND.findall(text)) | {kind for needle, kind in _EXTRA_ROUNDS
                                          if needle in text.replace(" ", "")}
    cohorts = set(_COHORT.findall(text))
    for match in re.finditer(r"(\d{1,3})\s*,\s*(\d{1,3})\s*기", text):
        cohorts.update(match.groups())
    return EditionTokens(frozenset(years), frozenset(terms), frozenset(halves),
                         frozenset(rounds), frozenset(cohorts))


def core_name(name: str, provider: str | None = None) -> str:
    """Program name without edition tokens, brackets, provider and generic words."""
    text = _BRACKETS.sub(" ", _nfkc(name))
    text = _REVISION.sub(" ", text)
    for pattern in (r"\d{1,2}\s*-\s*[12]\s*학기", r"(?<!\d)20\d{2}(?!\d)\s*(?:학년도|년도|년)?",
                    r"['’]?(?<!\d)\d{2}\s*(?:학년도|년도|년)",
                    r"(?<!\d)[12]\s*학기", r"\d{1,2}\s*차", r"\d{1,3}\s*(?:,\s*\d{1,3}\s*)*기(?![가-힣])"):
        text = re.sub(pattern, " ", text)
    for needle, _ in (*_TERMS, *_HALVES, *_EXTRA_ROUNDS):
        text = text.replace(needle, " ")
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    text = text.replace("장학생", "장학").replace("장학금", "장학")
    provider_key = re.sub(r"[^0-9a-z가-힣]+", "", _nfkc(provider or ""))
    if len(provider_key) >= 3 and provider_key in text and text != provider_key:
        text = text.replace(provider_key, "")
    for word in _FILLER:
        if text.endswith(word) and len(text) > len(word) + 1:
            text = text[: -len(word)]
    return text


def _trigrams(value: str) -> set[str]:
    padded = f"  {value} "
    return {padded[index:index + 3] for index in range(len(padded) - 2)}


def name_similarity(left: str, right: str) -> float:
    """Trigram Jaccard on core names; a long core contained in the other counts as close."""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    shorter, longer = sorted((left, right), key=len)
    if len(shorter) >= 6 and shorter in longer:
        return 0.9
    a, b = _trigrams(left), _trigrams(right)
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class EditionSignature:
    name: str
    core: str
    tokens: EditionTokens
    academic_year: int | None = None
    academic_term: str | None = None
    round_label: str | None = None
    deadlines: tuple[date, ...] = ()
    starts: tuple[date, ...] = ()

    @property
    def fact_years(self) -> frozenset[int]:
        return frozenset({self.academic_year}) if self.academic_year else frozenset()

    @property
    def fact_terms(self) -> frozenset[str]:
        known = {"spring", "fall", "summer", "winter"}
        return frozenset({self.academic_term}) if self.academic_term in known else frozenset()

    @property
    def rounds(self) -> frozenset[str]:
        extra = edition_tokens(self.round_label).rounds if self.round_label else frozenset()
        return self.tokens.rounds | extra


def signature(name: str, *, provider: str | None = None, academic_year: int | None = None,
              academic_term: str | None = None, round_label: str | None = None,
              deadlines: Iterable[date | None] = (), starts: Iterable[date | None] = ()) -> EditionSignature:
    return EditionSignature(
        name=name or "", core=core_name(name or "", provider),
        tokens=edition_tokens(" ".join(filter(None, [name, round_label]))),
        academic_year=academic_year, academic_term=academic_term, round_label=round_label,
        deadlines=tuple(sorted(item for item in deadlines if item)),
        starts=tuple(sorted(item for item in starts if item)))


@dataclass(frozen=True)
class Comparison:
    verdict: str
    name_score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    rule_version: str = RULE_VERSION

    def as_json(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "name_score": round(self.name_score, 4),
                "reasons": list(self.reasons), "rule_version": self.rule_version}


def _conflict(label: str, left: frozenset, right: frozenset, reasons: list[str]) -> bool:
    if left and right and not (left & right):
        reasons.append(f"{label} differs: {sorted(left)} vs {sorted(right)}")
        return True
    return False


def compare(left: EditionSignature, right: EditionSignature) -> Comparison:
    score = name_similarity(left.core, right.core)
    reasons = [f"core name similarity {score:.2f} ({left.core} / {right.core})"]
    if score < RELATED_NAME:
        return Comparison("different", score, tuple(reasons))
    # Extracted facts and name tokens are compared separately so one cannot mask the other.
    conflicts = [
        _conflict("academic year", left.fact_years, right.fact_years, reasons),
        _conflict("year in name", left.tokens.years, right.tokens.years, reasons),
        _conflict("academic term", left.fact_terms, right.fact_terms, reasons),
        _conflict("term in name", left.tokens.terms, right.tokens.terms, reasons),
        _conflict("half", left.tokens.halves, right.tokens.halves, reasons),
        _conflict("round", left.rounds, right.rounds, reasons),
        _conflict("cohort", left.tokens.cohorts, right.tokens.cohorts, reasons),
    ]
    if left.deadlines and right.deadlines:
        gap = min(abs((a - b).days) for a in left.deadlines for b in right.deadlines)
        if gap > DEADLINE_GAP_DAYS:
            reasons.append(f"application deadlines are {gap} days apart")
            conflicts.append(True)
    if any(conflicts):
        return Comparison("different_edition", score, tuple(reasons))
    one_sided = [label for label, a, b in (("round", left.rounds, right.rounds),
                                           ("cohort", left.tokens.cohorts, right.tokens.cohorts))
                 if bool(a) != bool(b)]
    if one_sided:
        reasons.append(f"{'/'.join(one_sided)} marker on one side only")
        return Comparison("uncertain", score, tuple(reasons))
    anchors = []
    if (left.fact_years & right.fact_years) or (left.tokens.years & right.tokens.years):
        anchors.append("year")
    if left.deadlines and right.deadlines and set(left.deadlines) & set(right.deadlines):
        anchors.append("deadline")
    if left.starts and right.starts and set(left.starts) & set(right.starts):
        anchors.append("start")
    if anchors:
        reasons.append(f"matching anchors: {', '.join(anchors)}")
    else:
        reasons.append("no matching year or application date")
    if score >= SAME_NAME and anchors:
        return Comparison("same", score, tuple(reasons))
    return Comparison("uncertain", score, tuple(reasons))


def _fact(fact: Any) -> Any:
    return fact.value if fact is not None and fact.state in {"stated", "resolved_update"} else None


def draft_signature(draft: Any, term_of: Any) -> EditionSignature:
    """Signature of an extracted OpportunityDraft; ``term_of`` maps semester text to a term."""
    deadlines, starts = [], []
    for window in draft.application_windows:
        if window.stage not in {"application", "additional_application", "nomination"}:
            continue
        end, start = _fact(window.end), _fact(window.start)
        deadlines.append(end.date if end else None)
        starts.append(start.date if start else None)
    return signature(_fact(draft.name) or "", provider=_fact(draft.organization),
                     academic_year=_fact(draft.academic_year),
                     academic_term=term_of(_fact(draft.semester)),
                     round_label=_fact(draft.round_label), deadlines=deadlines, starts=starts)


async def version_signature(session: Any, row: Any) -> EditionSignature:
    """Signature of a stored opportunity version row (needs id, title, provider, year, term, round)."""
    from sqlalchemy import text

    windows = (await session.execute(text("""
        SELECT start_date, end_date FROM inha_policy.application_windows
        WHERE opportunity_version_id=:id
          AND window_kind IN ('application', 'additional_application', 'nomination')
    """), {"id": row["id"]})).mappings().all()
    return signature(row["title"], provider=row["provider_name"],
                     academic_year=row["academic_year"], academic_term=row["academic_term"],
                     round_label=row["round_label"],
                     deadlines=[item["end_date"] for item in windows],
                     starts=[item["start_date"] for item in windows])
