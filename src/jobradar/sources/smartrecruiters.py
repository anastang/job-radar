"""SmartRecruiters job board adapter.

Unlike the other sources, SmartRecruiters exposes a structured ``experienceLevel``
(``entry_level`` / ``mid_senior_level`` / ``director`` / ...), which is a far more
reliable seniority signal than parsing prose. It is carried through in ``extra`` and
consumed by filters.py.

The list endpoint omits descriptions - fetching them costs one request per posting,
which is not worth it on the 5-minute hot path. Filters treat a missing description
as "unknown" rather than as a rejection.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt
from .base import Fetcher

ATS = "smartrecruiters"
URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
PAGE = 100
MAX_PAGES = 10

BLOCKING_LEVELS = {"director", "executive"}


def _location(raw: dict[str, Any]) -> tuple[str, bool]:
    loc = raw.get("location") or {}
    parts = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    text = ", ".join(p for p in parts if p)
    remote = bool(loc.get("remote"))
    if remote:
        text = f"{text}; Remote" if text else "Remote"
    return text, remote


def parse(company: str, items: list[dict[str, Any]]) -> list[Job]:
    jobs: list[Job] = []
    for raw in items:
        job_id = raw.get("id")
        if job_id is None:
            continue
        if (raw.get("visibility") or "PUBLIC").upper() != "PUBLIC":
            continue

        identifier = (raw.get("company") or {}).get("identifier") or company
        location, remote = _location(raw)
        level = (raw.get("experienceLevel") or {}).get("id") or ""

        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=str(job_id),
                title=(raw.get("name") or "").strip(),
                url=f"https://jobs.smartrecruiters.com/{identifier}/{job_id}",
                location_raw=location,
                posted_at=parse_dt(raw.get("releasedDate")),
                department=(raw.get("department") or {}).get("label"),
                employment_type=(raw.get("typeOfEmployment") or {}).get("label"),
                is_remote=remote,
                extra={
                    "experience_level": level,
                    "blocking_level": level in BLOCKING_LEVELS,
                    "function": (raw.get("function") or {}).get("label"),
                },
            )
        )
    return jobs


async def fetch(fetcher: Fetcher, company: str) -> list[Job]:
    base = URL.format(company=company)
    jobs: list[Job] = []
    offset = 0
    for _ in range(MAX_PAGES):
        data = await fetcher.get_json(f"{base}?limit={PAGE}&offset={offset}")
        if not isinstance(data, dict):
            break
        items = data.get("content") or []
        if not items:
            break
        jobs.extend(parse(company, items))
        offset += PAGE
        if offset >= int(data.get("totalFound") or 0):
            break
    return jobs
