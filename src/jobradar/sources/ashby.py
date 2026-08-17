"""Ashby job board adapter.

Ashby gives the richest payload of any source - full plain-text description,
structured compensation, remote flag, and secondary locations - all in one call.

Critical: this endpoint 404s on every request without a browser User-Agent. That
header comes from ``Fetcher``'s defaults; do not bypass it.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt
from .base import Fetcher

ATS = "ashby"
URL = "https://api.ashbyhq.com/posting-api/job-board/{company}?includeCompensation=true"


def _locations(raw: dict[str, Any]) -> str:
    parts = [raw.get("location") or ""]
    for sec in raw.get("secondaryLocations") or []:
        if name := (sec or {}).get("location"):
            parts.append(name)
    return "; ".join(p for p in parts if p)


def _salary(raw: dict[str, Any]) -> tuple[float | None, float | None, str | None]:
    comp = raw.get("compensation") or {}
    for tier in comp.get("compensationTiers") or []:
        for component in tier.get("components") or []:
            if (component.get("compensationType") or "").lower() != "salary":
                continue
            lo, hi = component.get("minValue"), component.get("maxValue")
            if lo is None and hi is None:
                continue
            return lo, hi, component.get("currencyCode")
    return None, None, None


def parse(company: str, payload: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload.get("jobs") or []:
        job_id = raw.get("id")
        if job_id is None:
            continue
        # isListed=False means the posting is unlisted/internal - not applyable.
        if raw.get("isListed") is False:
            continue

        lo, hi, currency = _salary(raw)
        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=str(job_id),
                title=(raw.get("title") or "").strip(),
                url=raw.get("jobUrl") or "",
                apply_url=raw.get("applyUrl"),
                location_raw=_locations(raw),
                description=raw.get("descriptionPlain") or "",
                posted_at=parse_dt(raw.get("publishedAt")),
                updated_at=parse_dt(raw.get("updatedAt")),
                department=raw.get("department"),
                employment_type=raw.get("employmentType"),
                workplace_type=raw.get("workplaceType"),
                is_remote=bool(raw.get("isRemote")),
                salary_min=lo,
                salary_max=hi,
                salary_currency=currency,
                extra={"team": raw.get("team")},
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
