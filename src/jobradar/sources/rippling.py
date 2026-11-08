"""Rippling ATS job board adapter.

Rippling is the one provider here that splits what we need across two endpoints. The
board listing is cheap but thin - uuid, title, department, one location string - and
carries **no posting date and no description at all**. Both live on a per-posting
detail endpoint.

That shape matters, because without a date every Rippling posting would bypass the
staleness gate and score as unknown-age forever, and without a description the
generic-engineering gate could never admit one. So this adapter fetches details.

What makes that affordable is filtering on the title first. Measured on Rippling's
own board, 665 live postings reduce to 19 once senior, intern and plainly irrelevant
titles are dropped: 2.9%. One list request plus roughly twenty small detail requests
per board is a fair cost for real dates and real descriptions, and it is far cheaper
than the 665 it would take to fetch everything.

The pre-filter deliberately re-uses the same title predicates the real gate applies
later, so it can only ever discard postings that `evaluate` would have rejected
anyway. Anything it keeps still goes through every gate in full.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..filters import (
    INTERNSHIP_TITLE,
    MTS_TITLE,
    NEGATIVE_TITLE,
    SENIOR_TITLE,
    SWE_TITLE,
    classify_role,
)
from ..models import Job, parse_dt, strip_html
from .base import Fetcher

ATS = "rippling"
BASE = "https://api.rippling.com/platform/api/ats/v1/board/{company}"

# Detail requests are issued concurrently per board, but capped so one unusually
# large board cannot monopolise the shared connection pool.
DETAIL_CONCURRENCY = 6

# Rippling reports pay by period. Only annual figures are comparable to the salary
# numbers every other source gives us, so anything else is left unset rather than
# published at the wrong magnitude - a monthly 10,500 shown as a salary would read
# as an insultingly low offer instead of a competitive one.
ANNUAL = "YEAR"


def title_is_worth_detail(title: str) -> bool:
    """Whether a listing's title justifies spending a detail request on it.

    Mirrors the title-level gates in filters.py. A title rejected here would be
    rejected by `evaluate` too, so this cannot hide a posting that would have
    alerted.
    """
    text = (title or "").strip()
    if not text:
        return False
    if SENIOR_TITLE.search(text) or INTERNSHIP_TITLE.search(text):
        return False
    if NEGATIVE_TITLE.search(text):
        return False
    return bool(classify_role(text) or SWE_TITLE.search(text) or MTS_TITLE.search(text))


def _description(raw: Any) -> str:
    """Flatten Rippling's sectioned description into plain text.

    The field is an object keyed by section ("company", "role", ...) rather than a
    string, and each section holds HTML.
    """
    if isinstance(raw, str):
        return strip_html(raw)
    if isinstance(raw, dict):
        parts = [strip_html(v) for v in raw.values() if isinstance(v, str)]
        return "\n\n".join(p for p in parts if p)
    return ""


def _salary(ranges: Any) -> tuple[float | None, float | None, str | None]:
    """Widest annual range across the per-location bands Rippling publishes."""
    if not isinstance(ranges, list):
        return None, None, None
    lows: list[float] = []
    highs: list[float] = []
    currency: str | None = None
    for band in ranges:
        if not isinstance(band, dict) or (band.get("frequency") or "").upper() != ANNUAL:
            continue
        start, end = band.get("rangeStart"), band.get("rangeEnd")
        if isinstance(start, (int, float)) and start > 0:
            lows.append(float(start))
        if isinstance(end, (int, float)) and end > 0:
            highs.append(float(end))
        currency = currency or band.get("currency")
    return (min(lows) if lows else None, max(highs) if highs else None, currency)


def _location(listing: dict[str, Any], detail: dict[str, Any]) -> str:
    """Prefer the detail endpoint's full list over the listing's single label."""
    places = detail.get("workLocations")
    if isinstance(places, list):
        text = "; ".join(str(p).strip() for p in places if str(p).strip())
        if text:
            return text
    one = listing.get("workLocation")
    if isinstance(one, dict):
        return str(one.get("label") or "").strip()
    return str(one or "").strip()


def build(company: str, listing: dict[str, Any], detail: dict[str, Any]) -> Job | None:
    uuid = listing.get("uuid") or detail.get("uuid")
    if not uuid:
        return None
    # Rippling marks postings hidden from its own search rather than removing them.
    if detail.get("unlistedFromSearch") is True:
        return None

    title = str(detail.get("name") or listing.get("name") or "").strip()
    if not title:
        return None

    low, high, currency = _salary(detail.get("payRangeDetails"))
    location = _location(listing, detail)

    # The readable value sits in `id`, not `label`: {"label": "SALARIED_FT",
    # "id": "Salaried, full-time"}. The gate reads this to catch postings typed as
    # an internship whose title does not say so.
    employment = detail.get("employmentType")
    employment_type = None
    if isinstance(employment, dict):
        employment_type = employment.get("id") or employment.get("label")

    department = listing.get("department") or detail.get("department")
    if isinstance(department, dict):
        department = department.get("label") or department.get("id")

    return Job(
        ats=ATS,
        company=company,
        external_id=str(uuid),
        title=title,
        url=str(detail.get("url") or listing.get("url") or "").strip(),
        location_raw=location,
        posted_at=parse_dt(detail.get("createdOn")),
        description=_description(detail.get("description")),
        department=str(department).strip() if department else None,
        employment_type=str(employment_type).strip() if employment_type else None,
        is_remote="remote" in location.lower(),
        salary_min=low,
        salary_max=high,
        salary_currency=currency,
    )


async def probe(fetcher: Fetcher, company: str) -> list[str]:
    """Titles on a board, in one request, for slug discovery.

    Discovery only needs to know whether a board exists and has postings. Running
    the full `fetch` for that would spend a detail request per candidate title
    against boards that mostly turn out not to exist.
    """
    listings = await fetcher.get_json(f"{BASE.format(company=company)}/jobs")
    if not isinstance(listings, list):
        return []
    return [
        str(item.get("name") or "").strip()
        for item in listings
        if isinstance(item, dict) and item.get("name")
    ]


async def fetch(fetcher: Fetcher, company: str) -> list[Job]:
    listings = await fetcher.get_json(f"{BASE.format(company=company)}/jobs")
    if not isinstance(listings, list):
        return []

    candidates = [
        item for item in listings
        if isinstance(item, dict)
        and item.get("uuid")
        and title_is_worth_detail(str(item.get("name") or ""))
    ]
    if not candidates:
        return []

    gate = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def detail(item: dict[str, Any]) -> Job | None:
        async with gate:
            data = await fetcher.get_json(
                f"{BASE.format(company=company)}/jobs/{item['uuid']}"
            )
        if not isinstance(data, dict):
            return None
        return build(company, item, data)

    results = await asyncio.gather(*(detail(i) for i in candidates),
                                   return_exceptions=True)
    return [j for j in results if isinstance(j, Job)]
