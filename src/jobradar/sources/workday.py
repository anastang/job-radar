"""Workday CXS adapter.

Workday differs from every other source here in three ways that shape the design:

1. **It needs three identifiers, not one.** A board is addressed by host, tenant and
   site (``nvidia.wd5`` / ``nvidia`` / ``NVIDIAExternalCareerSite``). To keep
   ``companies.yaml`` a uniform list of strings, those are encoded in one slug as
   ``host/tenant/site`` and split here.

2. **Dates are prose.** ``postedOn`` reads "Posted 13 Days Ago", so freshness is
   day-granular at best. "Posted 30+ Days Ago" is deliberately treated as older than
   30 days, since that is the only thing it reliably tells us.

3. **Listing responses carry no description.** Fetching bodies would cost one request
   per posting, so instead of walking entire tenants we issue a handful of targeted
   searches. A tenant with thousands of postings costs a few requests rather than
   hundreds, and scoring handles the missing description by normalizing against the
   points actually available.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Job
from .base import Fetcher

ATS = "workday"
ENDPOINT = "https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
VIEW = "https://{host}.myworkdayjobs.com/en-US/{site}{path}"

PAGE = 20
MAX_PAGES = 5

# Only the role families we care about, so large tenants stay cheap to poll.
SEARCH_TERMS = (
    "data engineer",
    "analytics engineer",
    "data analyst",
    "ai engineer",
    "machine learning",
    "data scientist",
)

_DAYS = re.compile(r"(\d+)\+?\s*days?\s*ago", re.I)

# Workday collapses multi-location postings to "2 Locations" / "5 Locations", which
# tells the location filter nothing - roles in Lima, Bangalore and Madrid sailed
# through as "unknown location". The real place is in externalPath, e.g.
# "/job/Hyderabad-Telangana-India/Data-Engineer_R-123", so recover it from there.
_PLACEHOLDER_LOC = re.compile(r"^\s*\d+\s*locations?\s*$", re.I)


def location_from_path(path: str) -> str:
    """"/job/Lima-Peru/Data-Engineering_R-65103" -> "Lima Peru"."""
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "job":
        return ""
    return parts[1].replace("-", " ").strip()


def parse_posted_on(text: str | None) -> datetime | None:
    """Turn "Posted 13 Days Ago" into a timestamp.

    "30+ Days Ago" is mapped past the 30-day mark rather than to exactly 30, so a
    posting of genuinely unknown age is treated as stale instead of sneaking under
    the freshness gate.
    """
    if not text:
        return None
    lowered = text.lower()
    now = datetime.now(timezone.utc)
    if "today" in lowered or "just posted" in lowered:
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)
    match = _DAYS.search(lowered)
    if not match:
        return None
    days = int(match.group(1))
    if "+" in lowered:
        days = max(days + 15, days)
    return now - timedelta(days=days)


def split_slug(slug: str) -> tuple[str, str, str] | None:
    """``host/tenant/site`` -> parts. Returns None when malformed."""
    parts = [p for p in slug.split("/") if p]
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def parse(slug: str, payload: dict[str, Any]) -> list[Job]:
    parts = split_slug(slug)
    if parts is None:
        return []
    host, tenant, site = parts

    jobs: list[Job] = []
    for raw in payload.get("jobPostings") or []:
        path = raw.get("externalPath")
        if not path:
            continue
        location = (raw.get("locationsText") or "").strip()
        from_path = location_from_path(path)
        if not location or _PLACEHOLDER_LOC.match(location):
            location = from_path or location
        elif from_path and from_path.lower() not in location.lower():
            # Keep both: one may name the city, the other the country.
            location = f"{location}; {from_path}"

        jobs.append(
            Job(
                ats=ATS,
                company=tenant,
                external_id=path,
                title=(raw.get("title") or "").strip(),
                url=VIEW.format(host=host, site=site, path=path),
                location_raw=location,
                posted_at=parse_posted_on(raw.get("postedOn")),
                extra={"posted_on_raw": raw.get("postedOn")},
            )
        )
    return jobs


async def fetch(fetcher: Fetcher, slug: str) -> list[Job]:
    parts = split_slug(slug)
    if parts is None:
        return []
    host, tenant, site = parts
    url = ENDPOINT.format(host=host, tenant=tenant, site=site)

    found: dict[str, Job] = {}
    for term in SEARCH_TERMS:
        for page in range(MAX_PAGES):
            payload = {
                "appliedFacets": {},
                "limit": PAGE,
                "offset": page * PAGE,
                "searchText": term,
            }
            data = await fetcher.post_json(url, payload)
            if not isinstance(data, dict):
                break
            batch = parse(slug, data)
            for job in batch:
                # The same posting matches several search terms; keep one copy.
                found.setdefault(job.external_id, job)
            if len(batch) < PAGE or (page + 1) * PAGE >= int(data.get("total") or 0):
                break

    return list(found.values())
