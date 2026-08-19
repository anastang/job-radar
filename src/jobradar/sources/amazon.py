"""Amazon jobs adapter.

Amazon runs its own board rather than a third-party ATS, and it is large enough to
matter on its own: a single "data engineer" query returns several hundred openings.

The search endpoint is public JSON and returns qualifications inline, so seniority
parsing and skill matching work as well here as they do for Greenhouse. Like Workday,
the board is far too big to enumerate, so it is queried with a handful of targeted
searches instead and the results deduplicated by job path.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ..models import Job, strip_html
from .base import Fetcher

ATS = "amazon"
BASE = "https://www.amazon.jobs"
SEARCH = BASE + "/en/search.json?base_query={query}&result_limit={limit}&offset={offset}"

PAGE = 100
MAX_PAGES = 3

SEARCH_TERMS = (
    "data engineer",
    "business intelligence engineer",
    "data analyst",
    "analytics engineer",
    "machine learning engineer",
    "data scientist",
)

# "May 21, 2026"
_POSTED = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def parse_posted(value: str | None) -> datetime | None:
    if not value:
        return None
    match = _POSTED.search(value)
    if not match:
        return None
    month = _MONTHS.get(match.group(1))
    if not month:
        return None
    try:
        return datetime(int(match.group(3)), month, int(match.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None


def parse(payload: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload.get("jobs") or []:
        path = raw.get("job_path")
        if not path:
            continue

        # Qualifications carry the years requirement, which is what the seniority
        # gate needs; the description alone often omits it.
        description = " ".join(
            strip_html(raw.get(field) or "")
            for field in ("description", "basic_qualifications", "preferred_qualifications")
        ).strip()

        jobs.append(
            Job(
                ats=ATS,
                company="amazon",
                external_id=path,
                title=(raw.get("title") or "").strip(),
                url=f"{BASE}{path}",
                location_raw=raw.get("normalized_location") or raw.get("location") or "",
                description=description,
                posted_at=parse_posted(raw.get("posted_date")),
                department=raw.get("business_category"),
                extra={"team": raw.get("team", {}).get("label") if isinstance(raw.get("team"), dict) else None},
            )
        )
    return jobs


async def fetch(fetcher: Fetcher, company: str = "amazon") -> list[Job]:
    """`company` is accepted for adapter-signature parity and otherwise unused."""
    found: dict[str, Job] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            url = SEARCH.format(
                query=term.replace(" ", "+"), limit=PAGE, offset=page * PAGE
            )
            data = await fetcher.get_json(url)
            if not isinstance(data, dict):
                break
            batch = parse(data)
            for job in batch:
                found.setdefault(job.external_id, job)
            if len(batch) < PAGE:
                break
    return list(found.values())
