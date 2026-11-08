"""Discover ATS boards from aggregator sitemaps.

    python scripts/discover_from_boards.py

Writes ``config/companies_boards.yaml``, merged with the other company files at
runtime.

Why this exists. ATS APIs have no global search, so the tool can only poll companies
it already knows by name. Maintaining that list by hand is the wrong job for a human:
most applications go to companies you have never heard of, and hearing of them is the
hard part. Aggregator sites have already solved exactly that, so this reads their
answer instead of asking anyone to curate one.

What makes it cheap is that both sites put the company and the job title **into the
URL**, and publish every URL in a sitemap:

    https://hiringtempo.com/company/stripe/jobs/account-executive-platforms-grower-060e3115
    https://startup.jobs/forward-deployed-software-engineer-palantir-23813

So a handful of sitemap fetches yield tens of thousands of company-and-title pairs
with no page scraping at all, and the titles let us keep only companies that are
hiring for something relevant right now. Measured on hiringtempo: 51,065 job URLs,
3,197 with a relevant-looking title, behind 378 distinct companies, of which **190
were not in the company list at all** - Cerebras, CoreWeave, Chainguard, Airwallex,
Abnormal Security, CircleCI and Anaplan among them.

Both sites' robots.txt permit the sitemaps and the public job pages. Neither site's
API is touched: hiringtempo disallows ``/api/`` and startup.jobs disallows the apply
paths, and nothing here requests either. The job pages themselves are never fetched,
because the ATS the company already runs is a better source for the posting than an
aggregator's copy of it, and polling it directly is what makes this tool early.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import importlib.util
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.filters import (  # noqa: E402
    INTERNSHIP_TITLE,
    NEGATIVE_TITLE,
    SENIOR_TITLE,
    SWE_TITLE,
    classify_role,
)
from jobradar.sources.base import Fetcher  # noqa: E402

# Slug guessing and the provider probe list are shared with add_company.py rather
# than duplicated, so a fix to either benefits both.
_spec = importlib.util.spec_from_file_location(
    "add_company", Path(__file__).resolve().parent / "add_company.py"
)
add_company = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(add_company)

SITEMAPS = {
    "hiringtempo": [
        "https://hiringtempo.com/sitemap/0.xml",
        "https://hiringtempo.com/sitemap/1.xml",
    ],
    "startupjobs": [
        f"https://cdn.startup.jobs/sitemaps/startupjobs/posts{n}.xml.gz"
        for n in ("", "1", "2", "3", "4", "5")
    ],
    # himalayas publishes a nested index; the three leaves are listed directly
    # rather than walked, so a change in their naming fails loudly instead of
    # quietly yielding nothing.
    "himalayas": [
        f"https://himalayas.app/sitemap-jobs{n}.xml.gz" for n in (1, 2, 3)
    ],
}

# startup.jobs also publishes the authoritative company-slug list, which is what
# makes its post URLs parseable at all. See split_startupjobs.
STARTUPJOBS_COMPANIES = [
    "https://cdn.startup.jobs/sitemaps/startupjobs/companies.xml.gz",
    "https://cdn.startup.jobs/sitemaps/startupjobs/companies1.xml.gz",
]

# hiringtempo: /company/<slug>/jobs/<title-slug>-<8 hex>
HIRINGTEMPO = re.compile(r"/company/([^/]+)/jobs/(.+?)-[0-9a-f]{8}/?$")
# himalayas: /companies/<slug>/jobs/<title-slug>, where the title sometimes carries
# the posting id glued on the end and sometimes does not.
HIMALAYAS = re.compile(r"/companies/([^/]+)/jobs/([^/?#]+)")
HIMALAYAS_ID = re.compile(r"-\d{5,}$")
# startup.jobs: /<title-slug>-<company-slug>-<numeric id>, all hyphen-separated with
# no delimiter between the two slugs - and company slugs contain hyphens themselves.
STARTUPJOBS = re.compile(r"startup\.jobs/(.+)-(\d+)/?$")
STARTUPJOBS_COMPANY = re.compile(r"startup\.jobs/company/([^/]+)/?$")
# How many trailing tokens of a post slug to try as the company name.
MAX_COMPANY_TOKENS = 5

# startup.jobs slugs carry numeric entity prefixes from a bulk import, so the same
# employer appears as "0020-paypal-inc", "100-salesforce-inc" and "1000-kla-
# corporation-2". Those prefixes have to go before slug guessing, or every candidate
# is nonsense. Only stripped when something is left over, so "100ms" survives whole.
_ENTITY_PREFIX = re.compile(r"^(?:\d+-)+")
# Trailing disambiguators from the same import: "-2", "-company", "-inc-3".
_ENTITY_SUFFIX = re.compile(r"(?:-(?:company|\d+))+$")

_LOC = re.compile(r"<loc>([^<]+)</loc>")

# hiringtempo only, by default. All three aggregators work, but they are not equal.
#
# himalayas is the largest at 117,386 company-and-title pairs and 1,973 uncovered
# companies, but it aggregates remote listings and a large share of those companies
# are staffing and recruitment agencies - "4-staffing-corp", "a5-talent-finders",
# "5-star-global-recruitment-partners" - which scoring already values at zero. Worth
# running in batches, not worth making the default.
#
# As for the other two:
# hiringtempo yields 401 companies hiring relevant roles behind clean slugs
# ("cerebras", "chainguard"), of which ~205 are new, which is about 3,700 probe
# requests. startup.jobs yields 4,320 companies behind bulk-import slugs, which is
# roughly 72,000 probes even after cleaning - too much traffic to put on other
# people's endpoints for one job search, and most of it lands on dormant boards.
# Run it deliberately with --source startupjobs and a --limit when the cheaper
# source is exhausted.
DEFAULT_SOURCES = ["hiringtempo"]


def clean_company(slug: str) -> str:
    """Strip bulk-import noise from an aggregator's company slug."""
    text = (slug or "").strip().lower()
    stripped = _ENTITY_PREFIX.sub("", text)
    if stripped:
        text = stripped
    stripped = _ENTITY_SUFFIX.sub("", text)
    if stripped:
        text = stripped
    return text.replace("-", " ").strip()


def split_startupjobs(slug: str, companies: set[str]) -> tuple[str, str] | None:
    """Split "forward-deployed-software-engineer-palantir" into title and company.

    Nothing in the URL marks where the title ends and the company begins, and both
    halves contain hyphens. Guessing that the company is the last token produces
    garbage: real examples resolved to companies called "1", "10" and "091614".

    So the split is checked against the company slugs startup.jobs publishes in its
    own sitemap, longest match first - "general-catalyst" has to win over "catalyst".
    """
    tokens = slug.split("-")
    for take in range(min(MAX_COMPANY_TOKENS, len(tokens) - 1), 0, -1):
        candidate = "-".join(tokens[-take:])
        if candidate in companies:
            return "-".join(tokens[:-take]).replace("-", " "), candidate
    return None


def relevant_title(title: str) -> bool:
    """Whether a title slug suggests a role worth resolving the company for.

    Deliberately title-only and deliberately loose: this decides which *companies*
    to add, not which postings to alert on. Every posting found later still goes
    through the full gate, so a company admitted on a borderline title costs
    nothing but one board on the polling list.
    """
    text = (title or "").strip()
    if not text:
        return False
    if SENIOR_TITLE.search(text) or INTERNSHIP_TITLE.search(text):
        return False
    if NEGATIVE_TITLE.search(text):
        return False
    return bool(classify_role(text) or SWE_TITLE.search(text))


def parse_sitemap(
    source: str, body: str, companies: set[str] | None = None
) -> list[tuple[str, str]]:
    """(company slug, title text) pairs from one sitemap."""
    pairs: list[tuple[str, str]] = []
    for url in _LOC.findall(body):
        if source == "hiringtempo":
            match = HIRINGTEMPO.search(url)
            if match:
                pairs.append((match.group(1), match.group(2).replace("-", " ")))
            continue
        if source == "himalayas":
            match = HIMALAYAS.search(url)
            if match:
                title = HIMALAYAS_ID.sub("", match.group(2)).replace("-", " ")
                pairs.append((match.group(1), title))
            continue
        match = STARTUPJOBS.search(url)
        if not match:
            continue
        split = split_startupjobs(match.group(1), companies or set())
        if split:
            pairs.append((split[1], split[0]))
    return pairs


def normalize(slug: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (slug or "").lower())


def known_slugs(config_dir: Path) -> set[str]:
    """Every company already polled, normalized for comparison.

    Workday and Oracle slugs are compound ("host/tenant/site"), so only the leading
    segment is comparable to an aggregator's company slug.
    """
    known: set[str] = set()
    for path in sorted(config_dir.glob("companies*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for tiers in data.values():
            if not isinstance(tiers, dict):
                continue
            for slugs in tiers.values():
                for slug in slugs or []:
                    known.add(normalize(str(slug).split("/")[0]))
    return known


async def fetch_sitemap(fetcher: Fetcher, url: str) -> str:
    """Sitemaps are served gzipped or plain depending on the host and extension."""
    raw = await fetcher.get_bytes(url)
    if not raw:
        return ""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            return ""
    return raw.decode("utf-8", "replace")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(REPO / "config" / "companies_boards.yaml"))
    parser.add_argument("--limit", type=int, default=400,
                        help="most companies to resolve in one run")
    parser.add_argument("--source", choices=sorted(SITEMAPS), action="append",
                        help="aggregator to read (repeatable); defaults to "
                             "hiringtempo, see DEFAULT_SOURCES")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be resolved, make no probes")
    args = parser.parse_args()

    sources = args.source or DEFAULT_SOURCES
    already = known_slugs(Path(args.out).parent)
    by_ats: dict[str, set[str]] = {}

    async with Fetcher(concurrency=6, timeout=90) as fetcher:
        # startup.jobs post URLs cannot be split without its company-slug list.
        startupjobs_companies: set[str] = set()
        if "startupjobs" in sources:
            for url in STARTUPJOBS_COMPANIES:
                body = await fetch_sitemap(fetcher, url)
                # Match per extracted <loc> value, not against the raw XML: the
                # pattern is end-anchored and "</loc>" follows every URL in the body.
                for loc in _LOC.findall(body):
                    found = STARTUPJOBS_COMPANY.search(loc)
                    if found:
                        startupjobs_companies.add(found.group(1))
            print(f"startupjobs: {len(startupjobs_companies)} known company slugs")

        wanted: dict[str, str] = {}
        for source in sources:
            pairs: list[tuple[str, str]] = []
            for url in SITEMAPS[source]:
                pairs += parse_sitemap(
                    source, await fetch_sitemap(fetcher, url), startupjobs_companies
                )
            hits = {company for company, title in pairs if relevant_title(title)}
            fresh = {c for c in hits if normalize(c) not in already}
            print(f"{source}: {len(pairs)} job urls, {len(hits)} companies hiring "
                  f"relevant roles, {len(fresh)} not already polled")
            for company in fresh:
                name = clean_company(company)
                # A slug with no letters left is an import artefact, not a company.
                if len(name) >= 3 and re.search(r"[a-z]", name):
                    wanted.setdefault(normalize(name), name)

        names = sorted(wanted.values())[: args.limit]
        print(f"\nresolving {len(names)} companies against "
              f"{len(add_company.PROVIDERS)} providers")
        if args.dry_run:
            for name in names:
                print("  ", name)
            return

        results = await asyncio.gather(
            *(add_company.resolve(fetcher, n) for n in names), return_exceptions=True
        )

    hits = 0
    for result in results:
        if isinstance(result, BaseException) or result is None:
            continue
        ats, slug = result[0], result[1]
        by_ats.setdefault(ats, set()).add(slug)
        hits += 1
    rate = hits / len(names) * 100 if names else 0
    print(f"resolved {hits} boards ({rate:.0f}% hit rate)")

    out = Path(args.out)

    # Accumulate rather than replace. The candidate pool is far larger than one
    # run's --limit, so this file is built in batches, and a plain write made each
    # batch discard the last: the second run took it from 188 boards down to 126.
    # The lost boards were the smaller problem. Rewriting the file also un-knew
    # those companies, so `known_slugs` stopped excluding them and the next run
    # spent its whole budget re-resolving what it already had, thrashing instead of
    # growing. tier1 is carried through untouched in case a board was promoted.
    existing: dict[str, dict[str, list[str]]] = {}
    if out.exists():
        existing = yaml.safe_load(out.read_text(encoding="utf-8")) or {}

    merged: dict[str, dict[str, list[str]]] = {}
    for ats in sorted(set(existing) | set(by_ats)):
        prior = existing.get(ats) if isinstance(existing.get(ats), dict) else {}
        tier1 = list(prior.get("tier1") or [])
        tier2 = sorted(set(prior.get("tier2") or []) | by_ats.get(ats, set()))
        merged[ats] = {"tier1": tier1, "tier2": tier2}

    added = sum(len(v["tier2"]) for v in merged.values()) - sum(
        len(v.get("tier2") or []) for v in existing.values() if isinstance(v, dict)
    )

    lines = [
        "# Generated by scripts/discover_from_boards.py - do not hand-edit.",
        "# Companies found posting a relevant role on an aggregator sitemap, whose",
        "# own ATS board then resolved from their name. Merged with the other",
        "# companies*.yaml files at runtime.",
        "#",
        "# Each run ADDS to this file. The candidate pool needs many batches, so a",
        "# run that replaced the file would discard the previous batch and then",
        "# re-resolve it next time.",
        "",
    ]
    for ats in sorted(merged):
        lines.append(f"{ats}:")
        if merged[ats]["tier1"]:
            lines.append("  tier1:")
            lines.extend(f"    - {s}" for s in merged[ats]["tier1"])
        else:
            lines.append("  tier1: []")
        lines.append("  tier2:")
        lines.extend(f"    - {s}" for s in merged[ats]["tier2"])
        lines.append("")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    total = sum(len(v["tier2"]) for v in merged.values())
    print(f"wrote {out}: {total} boards (+{added} this run)")


if __name__ == "__main__":
    asyncio.run(main())
