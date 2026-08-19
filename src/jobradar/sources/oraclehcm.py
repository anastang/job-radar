"""Oracle HCM (Fusion Recruiting) adapter.

Oracle's recruiting cloud hosts a long tail of large employers, JPMorgan among them,
and roughly a tenth of the postings this tool could not previously reach. The REST
endpoint is uniform across every Oracle customer, so one adapter unlocks all of them.

A board is addressed by host and site number together, encoded in one slug as
``host/siteNumber`` (for example ``jpmc.fa.oraclecloud.com/CX_1``) so that
``companies.yaml`` stays a flat list of strings.

The list response gives a real ``PostedDate`` and a short description, but not the
full body. Scoring already handles thin descriptions by normalising against the
points available.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from ..models import Job, parse_dt, strip_html
from .base import Fetcher

ATS = "oraclehcm"
ENDPOINT = (
    "https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
    "?onlyData=true&expand=requisitionList&finder={finder}"
)
VIEW = "https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{job_id}"

PAGE = 100
MAX_PAGES = 3

SEARCH_TERMS = (
    "data engineer",
    "analytics engineer",
    "data analyst",
    "machine learning",
    "data scientist",
)


def split_slug(slug: str) -> tuple[str, str] | None:
    """``host/siteNumber`` -> parts. Returns None when malformed."""
    parts = [p for p in (slug or "").split("/") if p]
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def parse(slug: str, payload: dict[str, Any]) -> list[Job]:
    parts = split_slug(slug)
    if parts is None:
        return []
    host, site = parts
    # Company name is not in the payload, so derive it from the tenant subdomain.
    company = host.split(".")[0]

    jobs: list[Job] = []
    for item in payload.get("items") or []:
        for raw in item.get("requisitionList") or []:
            job_id = raw.get("Id")
            if job_id is None:
                continue
            jobs.append(
                Job(
                    ats=ATS,
                    company=company,
                    external_id=str(job_id),
                    title=(raw.get("Title") or "").strip(),
                    url=VIEW.format(host=host, site=site, job_id=job_id),
                    location_raw=(
                        raw.get("PrimaryLocation")
                        or raw.get("PrimaryLocationCountry")
                        or ""
                    ),
                    description=strip_html(raw.get("ShortDescriptionStr")),
                    posted_at=parse_dt(raw.get("PostedDate")),
                    employment_type=raw.get("WorkerType"),
                    workplace_type=raw.get("WorkplaceType") or None,
                    extra={"job_family": raw.get("JobFamily")},
                )
            )
    return jobs


async def fetch(fetcher: Fetcher, slug: str) -> list[Job]:
    parts = split_slug(slug)
    if parts is None:
        return []
    host, site = parts

    found: dict[str, Job] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            # The finder is a single comma-delimited argument and must be encoded
            # whole; an unescaped space in the keyword makes the URL invalid.
            finder = urllib.parse.quote(
                f"findReqs;siteNumber={site},limit={PAGE},"
                f"offset={page * PAGE},keyword={term}",
                safe="",
            )
            data = await fetcher.get_json(ENDPOINT.format(host=host, finder=finder))
            if not isinstance(data, dict):
                break
            batch = parse(slug, data)
            for job in batch:
                found.setdefault(job.external_id, job)
            if len(batch) < PAGE:
                break
    return list(found.values())
