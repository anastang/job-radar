"""SimplifyJobs New-Grad community feed.

This is the breadth backstop. Direct ATS polling only sees companies we have slugs
for; this curated feed catches new-grad postings at companies we don't track yet.

It is ~12.5 MB, so it is always fetched with a conditional GET and only re-parsed
when the ETag changes. It carries no description text, so seniority filtering for
this source leans on the title - acceptable, since the feed is already curated to
new-grad roles.
"""

from __future__ import annotations

from typing import Any

from ..models import Job, parse_dt
from .base import Fetcher

ATS = "simplify"
URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions"
    "/dev/.github/scripts/listings.json"
)

# Feed-native sponsorship values that are genuine blockers for a Canadian citizen
# on TN status. Plain "does not offer sponsorship" is NOT a blocker - see filters.py.
BLOCKING_SPONSORSHIP = {
    "u.s. citizenship is required",
    "us citizenship is required",
    "security clearance required",
}


def parse(payload: list[dict[str, Any]]) -> list[Job]:
    jobs: list[Job] = []
    for raw in payload:
        job_id = raw.get("id")
        if job_id is None:
            continue
        # The feed keeps historical rows around; only live ones are worth alerting on.
        if raw.get("active") is False or raw.get("is_visible") is False:
            continue

        locations = raw.get("locations") or []
        sponsorship = (raw.get("sponsorship") or "").strip()

        jobs.append(
            Job(
                ats=ATS,
                company=raw.get("company_name") or "unknown",
                external_id=str(job_id),
                title=(raw.get("title") or "").strip(),
                url=raw.get("url") or "",
                location_raw="; ".join(locations),
                posted_at=parse_dt(raw.get("date_posted")),
                updated_at=parse_dt(raw.get("date_updated")),
                extra={
                    "sponsorship": sponsorship,
                    "degrees": raw.get("degrees") or [],
                    "category": raw.get("category"),
                    "blocking_sponsorship": sponsorship.lower() in BLOCKING_SPONSORSHIP,
                },
            )
        )
    return jobs


async def fetch(fetcher: Fetcher) -> list[Job]:
    data = await fetcher.get_json(URL, etag_key=f"{ATS}:listings")
    if not isinstance(data, list):
        return []
    return parse(data)
