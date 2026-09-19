from __future__ import annotations

import re
from datetime import date

# Platforms to skip entirely - their application flows aren't supported.
EXCLUDED_URL_MARKERS = ("workday", "myworkdayjobs")

# Don't apply to roles asking for more experience than the candidate has
# room for; a posting whose *minimum* requirement is above this is a
# mismatch regardless of how well the rest of the description reads.
MAX_MIN_EXPERIENCE_YEARS = 7

_YEARS_PATTERNS = (
    # "7+ years", "7 + yrs", "7plus years"
    r"(\d{1,2})\s*\+\s*(?:years|yrs|year)",
    r"(\d{1,2})\s*plus\s*(?:years|yrs|year)",
    # "minimum of 8 years", "at least 8 years", "min 8 yrs"
    r"(?:minimum|min\.?|at\s+least)\s*(?:of\s*)?(\d{1,2})\s*(?:years|yrs|year)",
    # "8-12 years", "8 to 12 years" -> the lower bound is the minimum
    r"(\d{1,2})\s*(?:-|–|to)\s*\d{1,2}\s*(?:years|yrs|year)",
    # "8 years of experience"
    r"(\d{1,2})\s*(?:years|yrs)\s*(?:of\s*)?(?:relevant\s*|total\s*|overall\s*)?experience",
)


MAX_JOB_AGE_DAYS = 3


def is_older_than(date_posted: object, max_days: int = MAX_JOB_AGE_DAYS) -> bool:
    """True when a scraped posting date is more than max_days old.

    The LinkedIn search is already limited to the last 72 hours; this backs
    it up for postings whose date comes through. A missing date isn't treated
    as old - the search filter already applied to it.
    """
    if date_posted is None:
        return False
    try:
        posted = date.fromisoformat(str(date_posted).strip()[:10])
    except ValueError:
        return False
    return (date.today() - posted).days > max_days


def is_excluded_application_url(url: str) -> bool:
    """True for application URLs on platforms we deliberately don't handle."""
    if not url:
        return False
    lowered = url.lower()
    return any(marker in lowered for marker in EXCLUDED_URL_MARKERS)


def min_experience_required(text: str) -> int | None:
    """Smallest 'years of experience' figure a description asks for, or None
    when it doesn't state one.

    A posting usually mentions several numbers ("5+ years overall, 3+ years
    with Kubernetes"); the entry bar is the smallest of them, so that's what
    gets compared against the cap.
    """
    if not text:
        return None
    lowered = text.lower()
    found: list[int] = []
    for pattern in _YEARS_PATTERNS:
        for match in re.finditer(pattern, lowered):
            try:
                years = int(match.group(1))
            except (ValueError, IndexError):
                continue
            # Ignore absurd parses (e.g. a stray "2024 years").
            if 0 < years <= 30:
                found.append(years)
    return min(found) if found else None


def exceeds_experience_cap(
    text: str, cap: int = MAX_MIN_EXPERIENCE_YEARS
) -> tuple[bool, str]:
    """Whether a posting's stated minimum experience is above the cap.

    Descriptions that never state a figure are kept - silence isn't a
    requirement, and dropping them would throw away most postings.
    """
    years = min_experience_required(text)
    if years is None:
        return False, ""
    if years > cap:
        return True, f"requires {years}+ years (cap is {cap})"
    return False, ""
