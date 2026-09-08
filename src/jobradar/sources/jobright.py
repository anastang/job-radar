"""jobright.ai new-grad feed adapter.

Unlike the SimplifyJobs feed, jobright maintains lists split by role family, so the
Data-Analysis list is curated to roughly the roles this tool is looking for rather
than to software engineering in general. That makes it a higher-signal breadth
source, at the cost of a markdown table instead of JSON.

Three quirks drive the handling:

* A row whose company cell is the continuation marker belongs to the company named
  in the row above. Treating each row independently would attribute those postings
  to a company literally called an arrow.
* Dates carry no year ("Aug 18"). A date that lands more than a few days in the
  future must belong to the previous year.
* The feed carries reposts. The same role appears many times over, each copy with
  its own listing id, so the rows have to be collapsed on what they describe rather
  than on the id.

Links point at jobright's own listing page rather than the employer's ATS, so these
postings carry no description and no trustworthy posting date. Both are handled the
same way as the Simplify feed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..models import Job
from .base import Fetcher

ATS = "jobright"

# Role-specific lists. The Data-Analysis one is the closest match to this profile;
# the software list is broader and contributes mostly through the filters.
FEEDS = (
    "https://raw.githubusercontent.com/jobright-ai/2025-Data-Analysis-New-Grad/master/README.md",
    "https://raw.githubusercontent.com/jobright-ai/2025-Software-Engineer-New-Grad/master/README.md",
)

CONTINUATION = "↳"  # the arrow marking "same company as the row above"

_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}
_DATE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2})")
_JOB_ID = re.compile(r"/jobs/info/([0-9a-f]+)", re.I)


def _cell_text(cell: str) -> str:
    """Strip markdown emphasis and links down to the visible text."""
    text = _LINK.sub(r"\1", cell)
    return text.replace("**", "").replace("*", "").strip()


def parse_posted(value: str, today: datetime | None = None) -> datetime | None:
    """"Aug 18" -> a UTC datetime, rolling back a year when the date is ahead."""
    now = today or datetime.now(timezone.utc)
    match = _DATE.search(value or "")
    if not match:
        return None
    month = _MONTHS.get(match.group(1))
    if not month:
        return None
    try:
        parsed = datetime(now.year, month, int(match.group(2)), tzinfo=timezone.utc)
    except ValueError:
        return None
    # A couple of days of slack absorbs timezone skew at a month boundary.
    if parsed > now + timedelta(days=3):
        try:
            parsed = parsed.replace(year=now.year - 1)
        except ValueError:
            return None
    return parsed


def parse(markdown: str) -> list[Job]:
    jobs: list[Job] = []
    last_company = ""

    for line in markdown.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        # Skip the header and its separator row.
        if cells[0].lower().startswith("company") or set(cells[0]) <= {"-", " "}:
            continue

        company_cell, title_cell, location_cell = cells[0], cells[1], cells[2]
        date_cell = cells[4] if len(cells) > 4 else cells[-1]

        company = last_company if CONTINUATION in company_cell else _cell_text(company_cell)
        if not company:
            continue
        last_company = company

        link = _LINK.search(title_cell)
        url = link.group(2) if link else ""
        title = _cell_text(title_cell)
        if not title or not url:
            continue

        # The listing id keeps the dedupe key stable even if the tracking query
        # string on the URL changes between refreshes.
        id_match = _JOB_ID.search(url)
        external_id = id_match.group(1) if id_match else url.split("?")[0]

        jobs.append(
            Job(
                ats=ATS,
                company=company,
                external_id=external_id,
                title=title,
                url=url,
                location_raw=_cell_text(location_cell),
                posted_at=parse_posted(date_cell),
                workplace_type=_cell_text(cells[3]) if len(cells) > 3 else None,
            )
        )
    return jobs


def collapse_repeats(jobs: list[Job]) -> list[Job]:
    """Keep one posting per (company, title, location).

    Some employers repost an identical role dozens of times, and every copy carries
    its own listing id, so id-based dedupe leaves all of them standing. Measured on
    the Data-Analysis feed, one company accounted for 39 identical "Data Analyst"
    rows and a second for 19: together 13% of the whole feed, all of it reaching the
    alert as separate jobs.

    Location belongs in the key because a real multi-site posting is worth seeing
    once per site. Gotion lists the same analyst role in Fremont and in Manteno, and
    those are two different jobs to apply to.

    The surviving row is picked by lowest listing id rather than by feed order, so
    the choice holds steady when the feed is reordered. Picking by position would
    hand the same role a different dedupe key on a later poll, and it would alert a
    second time.
    """
    best: dict[tuple[str, str, str], Job] = {}
    for job in jobs:
        key = (
            job.company.strip().lower(),
            job.title.strip().lower(),
            (job.location_raw or "").strip().lower(),
        )
        current = best.get(key)
        if current is None or job.external_id < current.external_id:
            best[key] = job
    return list(best.values())


async def fetch(fetcher: Fetcher) -> list[Job]:
    found: dict[str, Job] = {}
    for url in FEEDS:
        markdown = await fetcher.get_text(url, etag_key=f"{ATS}:{url}")
        if not markdown:
            continue
        for job in parse(markdown):
            found.setdefault(job.key, job)
    return collapse_repeats(list(found.values()))
