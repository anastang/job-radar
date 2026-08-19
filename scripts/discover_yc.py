"""Discover job boards for YC companies that are actively hiring.

Hand-seeding company slugs does not scale to startups, and startups are exactly
where being early pays most - a 15-person company's posting has a tiny applicant
pool compared to Stripe's. This walks the public YC directory, keeps companies that
are active, hiring, and in the target metros, guesses their ATS slug from the
company name and website domain, and keeps whatever actually resolves.

    python scripts/discover_yc.py

Writes ``config/companies_yc.yaml``, which run.py merges with ``companies.yaml``.
Kept in a separate file so re-running validate_companies.py cannot clobber it.

Everything lands in tier2: there are hundreds of these boards and polling them all
every five minutes would be inconsiderate to the ATS providers for little gain -
a 30-minute cadence is still far ahead of anyone browsing a job board.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import yaml
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.sources import ashby, greenhouse, lever, workable  # noqa: E402
from jobradar.sources.base import Fetcher  # noqa: E402

YC_DIRECTORY = "https://yc-oss.github.io/api/companies/all.json"

# Every major North American tech hub, matching the tier1 locations in filters.py.
#
# This used to keep only SF, NYC, Toronto, Palo Alto, Mountain View and Brooklyn,
# which made discovery stricter than the scoring it feeds. Once all major hubs began
# scoring equally, that narrow filter was silently discarding hiring startups in
# Seattle, Austin, Boston and elsewhere before they ever reached the ranking.
DEFAULT_METROS = (
    r"San Francisco|Bay Area|Palo Alto|Mountain View|Menlo Park|San Jose"
    r"|Santa Clara|Sunnyvale|New York|Brooklyn|Toronto|Waterloo|Kitchener"
    r"|Seattle|Bellevue|Austin|Boston|Cambridge|Denver|Boulder|Vancouver"
    r"|Montreal|Ottawa|Calgary|Chicago|Los Angeles|San Diego|Portland"
    r"|Atlanta|Dallas|Remote"
)

PROVIDERS = [
    (greenhouse.ATS, greenhouse.fetch),
    (ashby.ATS, ashby.fetch),
    (lever.ATS, lever.fetch),
    (workable.ATS, workable.fetch),
]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def candidate_slugs(company: dict[str, Any]) -> list[str]:
    """Guess ATS slugs from the company name and website domain.

    Boards are almost always named after the company with punctuation removed
    ("Modern Treasury" -> "moderntreasury") or hyphenated, and the website's second
    level domain is a good third guess when the display name has drifted.
    """
    name = (company.get("name") or "").strip().lower()
    out: list[str] = []
    if name:
        compact = _NON_ALNUM.sub("", name)
        hyphen = _NON_ALNUM.sub("-", name).strip("-")
        out.extend([compact, hyphen])

    website = company.get("website") or ""
    match = re.search(r"https?://(?:www\.)?([^./]+)", website)
    if match:
        out.append(_NON_ALNUM.sub("", match.group(1).lower()))

    # Drop empties, duplicates and slugs too short to be meaningful.
    seen: set[str] = set()
    return [s for s in out if len(s) >= 3 and not (s in seen or seen.add(s))]


def select_companies(data: list[dict[str, Any]], metros: str, limit: int | None) -> list[dict]:
    pattern = re.compile(metros, re.I)
    picked = [
        c for c in data
        if c.get("status") == "Active"
        and c.get("isHiring")
        and pattern.search(c.get("all_locations") or "")
    ]
    picked.sort(key=lambda c: -(c.get("team_size") or 0))
    return picked[:limit] if limit else picked


def already_configured() -> set[str]:
    """Slugs any companies*.yaml file already lists.

    A full sweep probes ~1,200 companies against four providers, which is roughly
    an hour of requests. Most of that is re-checking boards that resolved last time
    and have not moved, so skipping them makes periodic re-runs affordable. Companies
    that failed to resolve are still retried, since they may have started hiring.
    """
    slugs: set[str] = set()
    for path in (REPO / "config").glob("companies*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for tiers in data.values():
            if not isinstance(tiers, dict):
                continue
            for tier in ("tier1", "tier2"):
                slugs.update(s.lower() for s in (tiers.get(tier) or []))
    return slugs


async def resolve(fetcher: Fetcher, company: dict[str, Any]) -> tuple[str, str, int] | None:
    """First (ats, slug, count) that returns postings, or None."""
    for slug in candidate_slugs(company):
        for ats, fn in PROVIDERS:
            try:
                jobs = await fn(fetcher, slug)
            except Exception:
                continue
            if jobs:
                return ats, slug, len(jobs)
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "config" / "companies_yc.yaml"))
    parser.add_argument("--metros", default=DEFAULT_METROS,
                        help="regex matched against the company's locations")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap how many companies to probe (largest teams first)")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--skip-known", action="store_true",
                        help="skip companies whose board is already configured")
    args = parser.parse_args()

    async with Fetcher(concurrency=args.concurrency, timeout=30.0) as fetcher:
        directory = await fetcher.get_json(YC_DIRECTORY)
        if not isinstance(directory, list):
            raise SystemExit("Could not fetch the YC directory")

        companies = select_companies(directory, args.metros, args.limit)
        print(f"YC directory: {len(directory)} companies")
        print(f"Active + hiring + in target metros: {len(companies)}")

        known = already_configured() if args.skip_known else set()
        if known:
            before = len(companies)
            companies = [
                c for c in companies
                if not any(s in known for s in candidate_slugs(c))
            ]
            print(f"Skipping {before - len(companies)} already-configured companies")

        print(f"Probing {len(companies)} companies for job boards "
              f"(expect roughly a minute per 25)...")

        results = await asyncio.gather(
            *(resolve(fetcher, c) for c in companies), return_exceptions=True
        )

    by_ats: dict[str, set[str]] = {}
    resolved = 0
    for company, result in zip(companies, results):
        if isinstance(result, BaseException) or result is None:
            continue
        ats, slug, _ = result
        by_ats.setdefault(ats, set()).add(slug)
        resolved += 1

    # With --skip-known the sweep only sees new companies, so fold in whatever this
    # file already held. Rewriting it from a partial sweep would silently discard
    # every board found on previous runs.
    if args.skip_known:
        existing = yaml.safe_load(Path(args.out).read_text(encoding="utf-8"))             if Path(args.out).exists() else {}
        for ats, tiers in (existing or {}).items():
            if isinstance(tiers, dict):
                by_ats.setdefault(ats, set()).update(
                    s for tier in ("tier1", "tier2") for s in (tiers.get(tier) or [])
                )

    lines = [
        "# Generated by scripts/discover_yc.py - do not hand-edit.",
        "# YC companies that are active, hiring, and in a major North American hub.",
        "# Merged with companies.yaml at runtime; kept separate so that",
        "# validate_companies.py cannot overwrite it.",
        "",
    ]
    for ats in sorted(by_ats):
        lines.append(f"{ats}:")
        lines.append("  tier1: []")
        lines.append("  tier2:")
        lines.extend(f"    - {s}" for s in sorted(by_ats[ats]))
        lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nResolved {resolved} of {len(companies)} companies -> {out}")
    for ats in sorted(by_ats):
        print(f"  {ats:<16} {len(by_ats[ats])}")


if __name__ == "__main__":
    asyncio.run(main())
