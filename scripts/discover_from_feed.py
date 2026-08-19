"""Harvest Workday and Workable boards from the SimplifyJobs feed, then validate them.

Workday boards cannot be guessed: they are addressed by host, tenant *and* site
("ngc.wd1" / "ngc" / "Northrop_Grumman_External_Site"), and the site name follows no
predictable pattern. Rather than guess, this reads real posting URLs out of the
community feed - which is itself evidence that the board actively posts early-career
roles - and keeps the ones that validate.

Employers on ``filters.blocked_companies`` are dropped here rather than at filter
time. The feed skews heavily toward defense contractors, and there is no reason to
spend a poll on a board whose every posting would be rejected.

    python scripts/discover_from_feed.py

Writes ``config/companies_feed.yaml``, merged with the other company files at runtime.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import urllib.parse
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.sources import oraclehcm, workable, workday  # noqa: E402
from jobradar.sources.base import Fetcher  # noqa: E402
from jobradar.sources.simplify import URL as SIMPLIFY_URL  # noqa: E402

WORKDAY_RE = re.compile(
    r"https://([^.]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-zA-Z_-]+/)?([^/?#]+)"
)
WORKABLE_RE = re.compile(r"https://apply\.workable\.com/([^/?#]+)/")
ORACLE_RE = re.compile(
    r"https://([a-z0-9.-]*oraclecloud\.com)/hcmUI/CandidateExperience/[^/]+/sites/([^/?#]+)/"
)

# Only harvest boards that actually post in North America. The feed carries plenty of
# UK and EU employers whose every posting the location gate would reject anyway, and
# polling them is pure cost - especially on Workable, which sends no ETag, so each of
# its boards re-downloads in full on every single run.
NA_LOCATION = re.compile(
    r"USA|United States|Canada|Remote|\bUS\b|\bNY\b|\bCA\b|\bTX\b|\bWA\b|\bMA\b"
    r"|San Francisco|New York|Toronto|Seattle|Boston|Austin|Chicago|Denver",
    re.I,
)


def load_blocklist() -> list[str]:
    cfg = yaml.safe_load((REPO / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
    return [b.lower() for b in ((cfg.get("filters") or {}).get("blocked_companies") or [])]


def harvest(listings: list[dict], blocked: list[str]) -> tuple[set[str], set[str], set[str]]:
    workday_specs: set[str] = set()
    workable_accounts: set[str] = set()
    oracle_specs: set[str] = set()

    for job in listings:
        if not job.get("active"):
            continue
        company = (job.get("company_name") or "").lower()
        if any(b in company for b in blocked):
            continue
        if not NA_LOCATION.search(" ".join(job.get("locations") or [])):
            continue
        url = job.get("url") or ""

        if match := WORKDAY_RE.search(url):
            tenant, host, site = match.groups()
            # The path segment right after the locale is the site; skip locale-only URLs.
            if site and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", site):
                if not any(b in tenant.lower() for b in blocked):
                    workday_specs.add(f"{tenant}.{host}/{tenant}/{site}")
        elif match := WORKABLE_RE.search(url):
            workable_accounts.add(match.group(1))
        elif match := ORACLE_RE.search(url):
            # Oracle boards are addressed by host and site number together.
            oracle_specs.add(f"{match.group(1)}/{match.group(2)}")

    return workday_specs, workable_accounts, oracle_specs


async def check_workday(fetcher: Fetcher, spec: str) -> bool:
    """One cheap POST - a full fetch across every search term is far too expensive."""
    parts = workday.split_slug(spec)
    if parts is None:
        return False
    host, tenant, site = parts
    data = await fetcher.post_json(
        workday.ENDPOINT.format(host=host, tenant=tenant, site=site),
        {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": "data"},
    )
    return isinstance(data, dict) and bool(data.get("jobPostings"))


async def check_workable(fetcher: Fetcher, account: str) -> bool:
    return bool(await workable.fetch(fetcher, account))


async def check_oracle(fetcher: Fetcher, spec: str) -> bool:
    parts = oraclehcm.split_slug(spec)
    if parts is None:
        return False
    host, site = parts
    finder = urllib.parse.quote(f"findReqs;siteNumber={site},limit=1,keyword=data", safe="")
    data = await fetcher.get_json(oraclehcm.ENDPOINT.format(host=host, finder=finder))
    if not isinstance(data, dict):
        return False
    return any(item.get("requisitionList") for item in data.get("items") or [])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "config" / "companies_feed.yaml"))
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--max-workday", type=int, default=150)
    parser.add_argument("--max-workable", type=int, default=80)
    parser.add_argument("--max-oracle", type=int, default=80)
    args = parser.parse_args()

    blocked = load_blocklist()
    async with Fetcher(concurrency=args.concurrency, timeout=30.0) as fetcher:
        listings = await fetcher.get_json(SIMPLIFY_URL)
        if not isinstance(listings, list):
            raise SystemExit("Could not fetch the Simplify feed")

        wd_specs, wk_accounts, or_specs = harvest(listings, blocked)
        wd_list = sorted(wd_specs)[: args.max_workday]
        wk_list = sorted(wk_accounts)[: args.max_workable]
        or_list = sorted(or_specs)[: args.max_oracle]
        print(f"Harvested {len(wd_specs)} Workday, {len(wk_accounts)} Workable, "
              f"{len(or_specs)} Oracle HCM boards")
        print(f"Validating {len(wd_list)} + {len(wk_list)} + {len(or_list)} "
              f"(blocklist already applied)...")

        wd_ok, wk_ok, or_ok = await asyncio.gather(
            asyncio.gather(*(check_workday(fetcher, s) for s in wd_list),
                           return_exceptions=True),
            asyncio.gather(*(check_workable(fetcher, a) for a in wk_list),
                           return_exceptions=True),
            asyncio.gather(*(check_oracle(fetcher, s) for s in or_list),
                           return_exceptions=True),
        )

    workday_live = [s for s, ok in zip(wd_list, wd_ok) if ok is True]
    workable_live = [a for a, ok in zip(wk_list, wk_ok) if ok is True]
    oracle_live = [s for s, ok in zip(or_list, or_ok) if ok is True]

    lines = [
        "# Generated by scripts/discover_from_feed.py - do not hand-edit.",
        "# Workday and Workable boards harvested from real posting URLs in the",
        "# SimplifyJobs feed, then validated live. Blocked employers are excluded.",
        "",
    ]
    # Amazon runs its own board and takes no slug, so it is emitted unconditionally
    # rather than harvested.
    for ats, slugs in (("workday", workday_live), ("workable", workable_live),
                       ("oraclehcm", oracle_live), ("amazon", ["amazon"])):
        if not slugs:
            continue
        lines.append(f"{ats}:")
        lines.append("  tier1: []")
        lines.append("  tier2:")
        lines.extend(f"    - {s}" for s in slugs)
        lines.append("")

    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nValidated -> {out}")
    print(f"  workday    {len(workday_live)}")
    print(f"  workable   {len(workable_live)}")
    print(f"  oraclehcm  {len(oracle_live)}")
    print(f"  amazon     1")


if __name__ == "__main__":
    asyncio.run(main())
