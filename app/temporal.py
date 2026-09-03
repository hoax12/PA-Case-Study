"""Date resolution and policy window arithmetic.

UM criteria hinge on when something happened relative to something else, and the
dates arrive as written text. The hard part is not the arithmetic: it is refusing
to collapse "09/03/25" into one reading when two are plausible. Everything here
keeps every candidate reading alive so the caller can see when they disagree.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from pydantic import BaseModel


class ResolvedDate(BaseModel):
    """A written date and every calendar date it could mean.

    More than one candidate is the normal case for slash dates: 09/03/25 is
    September 3rd to a US clinic and March 9th elsewhere. Collapsing that to one
    reading is how a naive pipeline silently invents a fact.
    """

    raw: str
    candidates: list[date]

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1


MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date(raw: str | None) -> ResolvedDate | None:
    """Resolve written text to every date it could plausibly mean."""

    if not raw:
        return None
    text = raw.strip()
    iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        return _resolved(text, [_safe_date(year, month, day)])

    named = re.search(
        r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text
    )
    if named:
        month = MONTHS.get(named.group(1)[:4].casefold().rstrip(".")) or MONTHS.get(
            named.group(1)[:3].casefold()
        )
        if month:
            return _resolved(
                text, [_safe_date(int(named.group(3)), month, int(named.group(2)))]
            )

    slashed = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", text)
    if slashed:
        first, second, year_part = (int(part) for part in slashed.groups())
        year = year_part if year_part > 99 else 2000 + year_part
        candidates = []
        # Month-first and day-first are both live readings unless one is invalid.
        for month, day in ((first, second), (second, first)):
            resolved = _safe_date(year, month, day)
            if resolved and resolved not in candidates:
                candidates.append(resolved)
        return _resolved(text, candidates)
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _resolved(raw: str, candidates: list[date | None]) -> ResolvedDate | None:
    present = [item for item in candidates if item is not None]
    return ResolvedDate(raw=raw, candidates=present) if present else None


def parse_duration(duration: str) -> tuple[int, int, int]:
    """Parse the ISO-8601 subset the policy needs: years, months, weeks."""

    match = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?", duration.upper())
    if not match:
        raise ValueError(f"Unsupported duration: {duration}")
    years, months, weeks = (int(part) if part else 0 for part in match.groups())
    if not any((years, months, weeks)):
        raise ValueError(f"Empty duration: {duration}")
    return years, months, weeks


def shift(anchor: date, duration: str) -> date:
    """Add a policy duration to a date, clamping to a real calendar day."""

    years, months, weeks = parse_duration(duration)
    total_months = anchor.month - 1 + months + years * 12
    year = anchor.year + total_months // 12
    month = total_months % 12 + 1
    day = min(anchor.day, _days_in_month(year, month))
    return date(year, month, day) + timedelta(weeks=weeks)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
