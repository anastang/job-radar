"""Lever job board adapter.

Lever returns a bare JSON array (not an object) and stores ``createdAt`` as epoch
milliseconds. Salary range is structured when the company publishes one.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt
from .base import Fetcher

ATS = "lever"
URL = "https://api.lever.co/v0/postings/{company}?mode=json"


def parse(company: str, payload: list[dict[str, Any]]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload:
        job_id = raw.get("id")
        if job_id is None:
            continue
        categories = raw.get("categories") or {}
        salary = raw.get("salaryRange") or {}

        # Lever splits the posting across several fields; concatenating them gives
        # the filters a complete text body to search for years-of-experience.
        description = "\n".join(
            part
            for part in (
                raw.get("descriptionPlain"),
                raw.get("additionalPlain"),
                raw.get("openingPlain"),
            )
            if part
        )

        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=str(job_id),
                title=(raw.get("text") or "").strip(),
                url=raw.get("hostedUrl") or "",
                apply_url=raw.get("applyUrl"),
                location_raw=categories.get("location") or "",
                description=description,
                posted_at=parse_dt(raw.get("createdAt")),
                department=categories.get("department"),
                employment_type=categories.get("commitment"),
                workplace_type=raw.get("workplaceType"),
                is_remote=(raw.get("workplaceType") or "").lower() == "remote",
                salary_min=salary.get("min"),
                salary_max=salary.get("max"),
                salary_currency=salary.get("currency"),
                extra={"team": categories.get("team"), "country": raw.get("country")},
            )
        )
    return jobs


async def fetch(fetcher: Fetcher, company: str) -> list[Job]:
    data = await fetcher.get_json(
        URL.format(company=company), etag_key=f"{ATS}:{company}"
    )
    if not isinstance(data, list):
        return []
    return parse(company, data)
