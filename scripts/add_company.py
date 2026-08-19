"""Add a company to the watchlist by name, resolving its job board automatically.

This exists because of a measured failure. Of eight companies found by hand on
LinkedIn and heard back from, four were reachable through job boards this tool
already polls, and were missed anyway purely because nothing had told the tool those
companies existed. The capability was never the limit; the company list was.

Generated company files come from broad sweeps: the YC directory, VC portfolios, ATS
harvesting. None of them will ever cover a company you happen to hear about from a
friend, a news story, or a LinkedIn post. That is the whole point of this script.

    python scripts/add_company.py "Charta Health" "Distyl AI"

Names are turned into candidate slugs, probed against every provider, and whatever
resolves is appended to ``config/companies_watchlist.yaml``. That file is hand-owned:
no generator writes to it, so entries survive every refresh.

Watchlist companies land in tier1, because a company you added deliberately is worth
more attention than one that arrived from a sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.sources import (  # noqa: E402
    ashby, greenhouse, lever, smartrecruiters, workable,
)
from jobradar.sources.base import Fetcher  # noqa: E402

WATCHLIST = REPO / "config" / "companies_watchlist.yaml"

# Ordered by how commonly startups use them, so the first hit is usually the right one.
PROVIDERS = [
    ("ashby", ashby.fetch),
    ("greenhouse", greenhouse.fetch),
    ("lever", lever.fetch),
    ("workable", workable.fetch),
    ("smartrecruiters", smartrecruiters.fetch),
]

# Words companies routinely drop from their board slug.
_NOISE = re.compile(r"\b(ai|inc|llc|labs?|technologies|technology|health|software|co)\b")


def candidate_slugs(name: str) -> list[str]:
    """Guess board slugs from a display name.

    "Distyl AI" resolves as "distyl", "Charta Health" as "chartahealth", so both the
    full compaction and the noise-stripped form have to be tried.
    """
    low = name.strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "", low)
    hyphen = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    stripped = re.sub(r"[^a-z0-9]+", "", _NOISE.sub("", low))
    first = re.sub(r"[^a-z0-9]+", "", low.split()[0]) if low.split() else ""
    return [s for s in dict.fromkeys([compact, hyphen, stripped, first]) if len(s) >= 3]


async def resolve(fetcher: Fetcher, name: str) -> tuple[str, str, int] | None:
    """First (ats, slug, job_count) that returns postings, or None."""
    for slug in candidate_slugs(name):
        for ats, fetch in PROVIDERS:
            try:
                jobs = await fetch(fetcher, slug)
            except Exception:
                continue
            if jobs:
                return ats, slug, len(jobs)
    return None


def load_watchlist() -> dict[str, dict[str, list[str]]]:
    if not WATCHLIST.exists():
        return {}
    return yaml.safe_load(WATCHLIST.read_text(encoding="utf-8")) or {}


def save_watchlist(data: dict[str, dict[str, list[str]]]) -> None:
    lines = [
        "# Hand-maintained. No generator writes to this file, so entries here survive",
        "# every refresh of the other companies*.yaml files.",
        "#",
        "# Add companies with: python scripts/add_company.py \"Company Name\"",
        "# Everything here is tier1: a company added deliberately is worth more",
        "# attention than one that arrived from a broad sweep.",
        "",
    ]
    for ats in sorted(data):
        slugs = sorted(set(data[ats].get("tier1") or []))
        if not slugs:
            continue
        lines.append(f"{ats}:")
        lines.append("  tier1:")
        lines.extend(f"    - {s}" for s in slugs)
        lines.append("  tier2: []")
        lines.append("")
    WATCHLIST.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST.write_text("\n".join(lines), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve company names to job boards and add them to the watchlist"
    )
    parser.add_argument("names", nargs="+", help='e.g. "Charta Health" "Distyl AI"')
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would resolve without writing")
    args = parser.parse_args()

    async with Fetcher(concurrency=10, timeout=30.0) as fetcher:
        results = await asyncio.gather(
            *(resolve(fetcher, n) for n in args.names), return_exceptions=True
        )

    data = load_watchlist()
    added, missed = [], []
    for name, result in zip(args.names, results):
        if isinstance(result, BaseException) or result is None:
            missed.append(name)
            continue
        ats, slug, count = result
        bucket = data.setdefault(ats, {"tier1": [], "tier2": []})
        if slug in (bucket.get("tier1") or []):
            print(f"  already watched  {name}  ({ats}:{slug})")
            continue
        bucket.setdefault("tier1", []).append(slug)
        added.append((name, ats, slug, count))

    for name, ats, slug, count in added:
        print(f"  resolved         {name}  ->  {ats}:{slug}  ({count} open roles)")
    for name in missed:
        print(f"  NOT FOUND        {name}  (check the careers page for its board URL)")

    if added and not args.dry_run:
        save_watchlist(data)
        print(f"\nWrote {WATCHLIST}")
        print("Run jobradar to start polling them.")
    elif args.dry_run:
        print("\nDry run, nothing written.")


if __name__ == "__main__":
    asyncio.run(main())
