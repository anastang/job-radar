"""Greenhouse job board adapter.

Greenhouse is the highest-value source: it exposes ``first_published``, which is the
true posting timestamp rather than a last-touched date, and ``?content=true`` returns
the full description in the same call - no per-job fan-out required.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt, strip_html
from .base import Fetcher

ATS = "greenhouse"
URL = "https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"


def parse(company: str, payload: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload.get("jobs") or []:
        job_id = raw.get("id")
        if job_id is None:
            continue
        location = (raw.get("location") or {}).get("name") or ""
        departments = [
            d.get("name") for d in (raw.get("departments") or []) if d.get("name")
        ]
        offices = [o.get("name") for o in (raw.get("offices") or []) if o.get("name")]
        # Some boards leave location.name empty and only populate offices.
        if not location and offices:
            location = ", ".join(offices)

        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=str(job_id),
                title=(raw.get("title") or "").strip(),
                url=raw.get("absolute_url") or "",
                location_raw=location,
                description=strip_html(raw.get("content")),
                posted_at=parse_dt(raw.get("first_published")),
                updated_at=parse_dt(raw.get("updated_at")),
                department=departments[0] if departments else None,
                extra={"offices": offices, "departments": departments},
            )
        )
    return jobs


async def fetch(fetcher: Fetcher, company: str) -> list[Job]:
    data = await fetcher.get_json(
        URL.format(company=company), etag_key=f"{ATS}:{company}"
    )
    if not isinstance(data, dict):
        return []
    return parse(company, data)
