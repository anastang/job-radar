"""Workable job board adapter.

The public widget endpoint returns every posting for an account in one call, and
with ``details=true`` it includes the full HTML description - so seniority parsing
and skill matching work as well here as they do for Greenhouse.

``published_on`` is date-granular (``2026-07-13``) rather than a timestamp, so
freshness scoring for these postings resolves to the day rather than the hour.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt, strip_html
from .base import Fetcher

ATS = "workable"
URL = "https://apply.workable.com/api/v1/widget/accounts/{company}?details=true"


def _location(raw: dict[str, Any]) -> tuple[str, bool]:
    parts = [raw.get("city"), raw.get("state"), raw.get("country")]
    text = ", ".join(p for p in parts if p)
    remote = bool(raw.get("telecommuting"))
    if remote:
        text = f"{text}; Remote" if text else "Remote"
    return text, remote


def parse(company: str, payload: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload.get("jobs") or []:
        shortcode = raw.get("shortcode")
        if not shortcode:
            continue
        location, remote = _location(raw)
        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=str(shortcode),
                title=(raw.get("title") or "").strip(),
                url=raw.get("url") or raw.get("shortlink") or "",
                apply_url=raw.get("application_url"),
                location_raw=location,
                description=strip_html(raw.get("description")),
                posted_at=parse_dt(raw.get("published_on") or raw.get("created_at")),
                department=raw.get("department"),
                employment_type=raw.get("employment_type") or None,
                is_remote=remote,
                extra={"function": raw.get("function"), "industry": raw.get("industry")},
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
