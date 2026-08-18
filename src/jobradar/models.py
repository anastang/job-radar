"""Normalized job posting model shared by every source adapter."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_WS = re.compile(r"\s+")
_TAG = re.compile(r"<[^>]+>")

# Sources whose dates are NOT the employer's posting date.
#
# The SimplifyJobs feed reports when *Simplify* first indexed a posting, and never
# refreshes it when the employer re-posts. A real case: EXL's Data Engineer listing
# carried date_posted 2026-07-08 while the employer's own page said 2026-08-16 - the
# feed was 39 days stale. Trusting that would make a day-old job look like a
# month-old one, and the staleness gate would then discard it.
#
# Every other source reads the employer's own system: Greenhouse first_published,
# Ashby publishedAt, Lever createdAt, Workable published_on. Workday's "Posted 13
# Days Ago" is coarse but genuine, so it stays trusted.
UNTRUSTED_DATE_SOURCES = frozenset({"simplify"})


def parse_dt(value: Any) -> datetime | None:
    """Parse the several timestamp shapes the ATS APIs return.

    Greenhouse/Ashby give ISO-8601 strings, Lever gives epoch milliseconds, and the
    Simplify feed gives epoch seconds. Always returns tz-aware UTC or None.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # Lever uses epoch ms; Simplify uses epoch seconds. 1e11 sits well past any
        # plausible second-precision date but below every ms-precision one.
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            return parse_dt(int(raw))
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def strip_html(text: str | None) -> str:
    """Flatten an HTML description to plain text for keyword matching.

    Greenhouse returns ``content`` entity-encoded (``&lt;p&gt;`` rather than ``<p>``),
    so unescaping has to happen *before* tag stripping or every tag survives as
    literal text and pollutes keyword matching.
    """
    if not text:
        return ""
    out = html.unescape(text)
    out = _TAG.sub(" ", out)
    return _WS.sub(" ", html.unescape(out)).strip()


@dataclass
class Job:
    """A posting normalized across every ATS we poll."""

    ats: str
    company: str
    external_id: str
    title: str
    url: str
    location_raw: str = ""
    apply_url: str | None = None
    description: str = ""
    posted_at: datetime | None = None
    updated_at: datetime | None = None
    department: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    is_remote: bool = False
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Stable dedupe key. Must not depend on any field the ATS may edit later."""
        raw = f"{self.ats}|{self.company}|{self.external_id}".lower()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def best_date(self) -> datetime | None:
        return self.posted_at or self.updated_at

    @property
    def date_trusted(self) -> bool:
        """Whether ``best_date`` is the employer's posting date or merely an index date."""
        return self.ats not in UNTRUSTED_DATE_SOURCES

    @property
    def age_hours(self) -> float | None:
        dt = self.best_date
        if dt is None:
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600

    @property
    def haystack(self) -> str:
        """Lowercased title + location + description used by filters and scoring."""
        return f"{self.title}\n{self.location_raw}\n{self.description}".lower()

    @property
    def title_location(self) -> str:
        return f"{self.title}\n{self.location_raw}".lower()

    @property
    def apply_link(self) -> str:
        return self.apply_url or self.url
