from __future__ import annotations

import re
from calendar import monthrange
from datetime import date
from typing import Any
from datetime import datetime
from zoneinfo import ZoneInfo


# Residence is matched by 시·도; the stored values are whatever the notice wrote ("서울특별시", "광산구").
REGIONS = ("서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원", "충북", "충남",
           "전북", "전남", "경북", "경남", "제주")
REGION_ALIASES = {"충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전라남도": "전남",
                  "경상북도": "경북", "경상남도": "경남", "경기도": "경기", "강원도": "강원"}
REGION_PATTERN = re.compile(
    r"(" + "|".join([*REGION_ALIASES, *REGIONS]) + r")(?:특별시|광역시|특별자치시|특별자치도|도)?\s*"
    r"(?:거주|지역|사는|에\s*사는|출신|주민)")


def understand_query(query: str, today: date | None = None) -> dict[str, Any]:
    """Convert a small, deterministic Korean query subset into allowed filters.

    The original query still participates in lexical/vector retrieval. Unknown phrases
    remain text; no SQL fragments are accepted from this function.
    """
    current = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    filters: dict[str, Any] = {}
    if re.search(r"대학원생|석사|박사", query):
        filters["student_type"] = "graduate"
    elif re.search(r"대학생|학부생", query):
        filters["student_type"] = "undergraduate"
    benefits = ((r"생활비", "living_cost"), (r"등록금", "tuition"),
                (r"주거|기숙사", "housing"), (r"현물|서비스", "in_kind"))
    for pattern, value in benefits:
        if re.search(pattern, query):
            filters["benefit_type"] = value
            break
    if "이번 달" in query or "이번달" in query:
        filters["application_end_from"] = current
        filters["application_end_to"] = date(current.year, current.month,
                                               monthrange(current.year, current.month)[1])
    amount = re.search(r"(\d[\d,]*)\s*(만)?\s*원\s*(?:이상|넘는|초과)", query)
    if amount:
        number = int(amount.group(1).replace(",", ""))
        filters["min_amount"] = number * (10_000 if amount.group(2) else 1)
    bracket = re.search(r"소득\s*(\d{1,2})\s*구간\s*(?:이하|까지)", query)
    if bracket:
        filters["income_bracket_max"] = int(bracket.group(1))
    region = REGION_PATTERN.search(query)
    if region:
        filters["region"] = REGION_ALIASES.get(region.group(1), region.group(1))
    if re.search(r"모집\s*중|신청\s*가능|현재", query):
        filters["status"] = "active"
    return filters
