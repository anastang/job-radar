"""Source adapter registry."""

from __future__ import annotations

from . import ashby, greenhouse, lever, simplify, smartrecruiters
from .base import BROWSER_UA, Fetcher

# Per-company adapters: fetch(fetcher, company) -> list[Job]
ATS_ADAPTERS = {
    greenhouse.ATS: greenhouse.fetch,
    ashby.ATS: ashby.fetch,
    lever.ATS: lever.fetch,
    smartrecruiters.ATS: smartrecruiters.fetch,
}

# Global adapters take no company argument.
GLOBAL_ADAPTERS = {simplify.ATS: simplify.fetch}

__all__ = [
    "ATS_ADAPTERS",
    "GLOBAL_ADAPTERS",
    "BROWSER_UA",
    "Fetcher",
    "ashby",
    "greenhouse",
    "lever",
    "simplify",
    "smartrecruiters",
]
