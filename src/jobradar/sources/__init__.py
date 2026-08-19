"""Source adapter registry."""

from __future__ import annotations

from . import (
    amazon,
    ashby,
    greenhouse,
    jobright,
    lever,
    oraclehcm,
    simplify,
    smartrecruiters,
    workable,
    workday,
)
from .base import BROWSER_UA, Fetcher

# Per-company adapters: fetch(fetcher, slug) -> list[Job].
# Note that workday slugs are "host/tenant/site" rather than a bare company name;
# see workday.py for why. Everything downstream still treats them as opaque strings.
ATS_ADAPTERS = {
    greenhouse.ATS: greenhouse.fetch,
    ashby.ATS: ashby.fetch,
    lever.ATS: lever.fetch,
    smartrecruiters.ATS: smartrecruiters.fetch,
    workable.ATS: workable.fetch,
    workday.ATS: workday.fetch,
    # oraclehcm slugs are "host/siteNumber"; amazon takes no meaningful slug and
    # sweeps its own board by keyword.
    oraclehcm.ATS: oraclehcm.fetch,
    amazon.ATS: amazon.fetch,
}

# Global adapters take no company argument.
GLOBAL_ADAPTERS = {simplify.ATS: simplify.fetch, jobright.ATS: jobright.fetch}

__all__ = [
    "ATS_ADAPTERS",
    "GLOBAL_ADAPTERS",
    "BROWSER_UA",
    "Fetcher",
    "amazon",
    "ashby",
    "greenhouse",
    "jobright",
    "lever",
    "oraclehcm",
    "simplify",
    "smartrecruiters",
    "workable",
    "workday",
]
