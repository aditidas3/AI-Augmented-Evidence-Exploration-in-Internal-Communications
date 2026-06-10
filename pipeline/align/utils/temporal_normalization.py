from __future__ import annotations

import re
from typing import Any

MONTH_NAME_TO_NUM: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

TEMPORAL_TOKEN_SPLIT_RE = re.compile(r"[\s,/.\-]+")


def normalize_temporal_value(value: Any) -> str:
    """Normalize a date-ish value into ``YYYY-MM-DD`` for lexical comparison."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]

    year_match = re.match(r"^(\d{4})", raw)
    if not year_match:
        return raw

    year = int(year_match.group(1))
    month = 1
    day = 1
    saw_month = False
    saw_day = False

    for token in TEMPORAL_TOKEN_SPLIT_RE.split(raw[4:]):
        if not token:
            continue
        token_l = token.lower()
        if token_l in MONTH_NAME_TO_NUM:
            month = MONTH_NAME_TO_NUM[token_l]
            saw_month = True
            continue
        if token.isdigit():
            n = int(token)
            if not saw_month and 1 <= n <= 12:
                month = n
                saw_month = True
            elif 1 <= n <= 31 and not saw_day:
                day = n
                saw_day = True

    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_date_from_text(text: str) -> str:
    raw = str(text or "")
    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
    if iso_match:
        return iso_match.group(1)
    month_match = re.search(
        r"\b("
        r"January|February|March|April|May|June|July|August|September|October|November|December"
        r")\s+\d{1,2},\s+\d{4}\b",
        raw,
        re.IGNORECASE,
    )
    if month_match:
        return month_match.group(0)
    return ""


def extract_year_from_date_string(date_str: str) -> int | None:
    match = re.search(r"\b((?:19|20)\d{2})\b", str(date_str or ""))
    return int(match.group(1)) if match else None


def date_in_year_range(
    date_str: str,
    *,
    start: str | None,
    end: str | None,
) -> bool | None:
    year = extract_year_from_date_string(date_str)
    if year is None:
        return None

    start_year = extract_year_from_date_string(start or "") if start else None
    end_year = extract_year_from_date_string(end or "") if end else None
    if start_year is None and end_year is None:
        return None
    if start_year is not None and year < start_year:
        return False
    if end_year is not None and year > end_year:
        return False
    return True
