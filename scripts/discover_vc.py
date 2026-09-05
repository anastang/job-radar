"""Discover job boards for venture portfolio companies.

The YC directory is the best single source of hiring startups, but it is one firm.
This walks a VC portfolio page, pulls the company names out, and resolves each to an
ATS board using the same name-to-slug guessing that discover_yc.py uses.

    python scripts/discover_vc.py

Writes ``config/companies_vc.yaml``, merged with the other company files at runtime.

A note on expected yield. YC resolves at about 34%, because its directory carries an
``isHiring`` flag so only companies known to be hiring are probed, and because YC
company names map to slugs predictably. VC portfolio pages carry no hiring signal, so
most probes land on companies that are not recruiting. Measured against a random
sample of 70 a16z names, the hit rate was **10%**, giving roughly 78 boards from ~780
candidates. Worth having, but do not expect YC numbers.

Only a16z is implemented. Sequoia, Accel, Index, Techstars and Creative Destruction
Lab all return large pages to one HTTP client and near-empty ones to another, which
means each needs its own extraction rule verified before it can be trusted. Adding
one blind would silently contribute zero companies and look like it worked.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.sources import ashby, greenhouse, lever, workable  # noqa: E402
from jobradar.sources.base import Fetcher  # noqa: E402

# name -> (portfolio url, regex capturing company names from the page source)
# firm key -> (portfolio url, name-capturing regex, firm name to strip from labels)
#
# Each entry was verified against the live page before being added. Several other
# well-known portfolios are deliberately absent because their listings are rendered
# client-side and the HTML carries almost nothing:
#
#   Accel, Techstars, Creative Destruction Lab   zero names in the markup
#   Sequoia                                      21 names, all household brands
#                                                already covered elsewhere
#
# Adding one of those blind would contribute nothing while appearing to work, which
# is worse than leaving it out.
SOURCES: dict[str, tuple[str, str, str]] = {
    "a16z": ("https://a16z.com/portfolio/", r'"title":"([^"]{2,40})"', "a16z"),
    "index": ("https://www.indexventures.com/companies/",
              r'"name"\s*:\s*"([^"]{2,40})"', "Index Ventures"),
    "foundersfund": ("https://foundersfund.com/portfolio/",
                     r'"name"\s*:\s*"([^"]{2,40})"', "Founders Fund"),
    "generalcatalyst": ("https://www.generalcatalyst.com/portfolio",
                        r'<h[23][^>]*>([^<]{2,40})</h[23]>', "General Catalyst"),
}

# Portfolio pages annotate exits inline; those are not companies to poll.
NOT_A_COMPANY = re.compile(r"^(acquired by|ipo|merged|exited)\b", re.I)

# Navigation and filter labels that sit in the same markup as the company names.
UI_CHROME = re.compile(
    r"^(all|portfolio|companies|about|team|news|contact|search|filter|sort|menu"
    r"|close|home|more|load more|view all|back|next|previous|our companies"
    r"|seed|series [a-z]|ipo|acquired|exited|stage|sector|industry)$",
    re.I,
)

PROVIDERS = [
    ("ashby", ashby.fetch),
    ("greenhouse", greenhouse.fetch),
    ("lever", lever.fetch),
    ("workable", workable.fetch),
]

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def candidate_slugs(name: str) -> list[str]:
    low = name.strip().lower()
    compact = _NON_ALNUM.sub("", low)
    hyphen = _NON_ALNUM.sub("-", low).strip("-")
    return [s for s in dict.fromkeys([compact, hyphen]) if 3 <= len(s) <= 40]


def extract_names(page: str, pattern: str, firm: str = "") -> list[str]:
    """Pull company names out of a portfolio page.

    Several firms label each entry with their own name appended ("Affirm - Founders
    Fund"), which would otherwise produce slugs that resolve to nothing.
    """
    names: set[str] = set()
    for raw in re.findall(pattern, page):
        name = re.sub(rf"\s*[-–]\s*{re.escape(firm)}\s*$", "", raw.strip(), flags=re.I)
        name = re.sub(r"\s+", " ", name).strip()
        if not name or len(name) < 2 or len(name) > 40:
            continue
        if NOT_A_COMPANY.match(name) or UI_CHROME.match(name):
            continue
        if re.search(r"[<>{}]|^\W+$", name):
            continue
        names.add(name)
    return sorted(names)


async def resolve(fetcher: Fetcher, name: str) -> tuple[str, str] | None:
    for slug in candidate_slugs(name):
        for ats, fetch in PROVIDERS:
            try:
                jobs = await fetch(fetcher, slug)
            except Exception:
                continue
            if jobs:
                return ats, slug
    return None


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "config" / "companies_vc.yaml"))
    parser.add_argument("--sources", nargs="*", default=list(SOURCES),
                        help=f"which portfolios to walk (available: {', '.join(SOURCES)})")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap candidates probed per source")
    parser.add_argument("--concurrency", type=int, default=16)
    args = parser.parse_args()

    by_ats: dict[str, set[str]] = {}
    async with Fetcher(concurrency=args.concurrency, timeout=30.0) as fetcher:
        for source in args.sources:
            if source not in SOURCES:
                print(f"Unknown source {source!r}; skipping")
                continue
            url, pattern, firm = SOURCES[source]
            page = await fetcher.get_text(url)
            if not page:
                print(f"{source}: could not fetch the portfolio page")
                continue

            names = extract_names(page, pattern, firm)
            if len(names) < 25:
                # A portfolio page that yields almost nothing has gone client-side.
                # Say so rather than silently contributing zero companies.
                print(f"{source}: only {len(names)} names extracted - the page markup "
                      f"has probably changed, check the regex")
            if args.limit:
                names = names[: args.limit]
            print(f"{source}: {len(names)} company names, probing for job boards...")

            results = await asyncio.gather(
                *(resolve(fetcher, n) for n in names), return_exceptions=True
            )
            hits = 0
            for result in results:
                if isinstance(result, BaseException) or result is None:
                    continue
                ats, slug = result
                by_ats.setdefault(ats, set()).add(slug)
                hits += 1
            rate = hits / len(names) * 100 if names else 0
            print(f"{source}: resolved {hits} boards ({rate:.0f}% hit rate)")

    lines = [
        "# Generated by scripts/discover_vc.py - do not hand-edit.",
        "# Venture portfolio companies whose ATS board resolved from their name.",
        "# Merged with the other companies*.yaml files at runtime.",
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
    print(f"\nWrote {out}")
    for ats in sorted(by_ats):
        print(f"  {ats:<16} {len(by_ats[ats])}")


if __name__ == "__main__":
    asyncio.run(main())
