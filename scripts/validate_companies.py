"""Probe candidate company slugs across every ATS and emit config/companies.yaml.

Slugs rot - companies migrate between ATS vendors and rename their boards - so the
company list is generated from live probes rather than hand-maintained. Re-run this
periodically to prune dead entries and pick up moves.

    python scripts/validate_companies.py --out config/companies.yaml

Tier 1 companies are polled every run; tier 2 on a slower cadence (see config.yaml).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from jobradar.sources import ashby, greenhouse, lever, smartrecruiters  # noqa: E402
from jobradar.sources.base import Fetcher  # noqa: E402

# Companies whose data/AI teams are the highest-value targets. Everything else
# still gets polled, just less often.
TIER1 = {
    # Slugs recovered by variant probing - the obvious spelling does not resolve for
    # these, so keep the working form here rather than the company's display name.
    "doordashusa", "perplexity", "gleanwork", "vanta", "moderntreasury",
    "openai", "anthropic", "databricks", "snowflake", "stripe", "ramp", "brex",
    "plaid", "figma", "notion", "linear", "cursor", "anysphere", "scaleai",
    "cohere", "shopify", "wealthsimple", "datadog", "cloudflare", "mongodb",
    "confluent", "fivetran", "airbyte", "dbtlabs", "hex", "sigmacomputing",
    "hightouch", "census", "amplitude", "mixpanel", "posthog", "clickhouse",
    "cockroachlabs", "reddit", "pinterest", "instacart", "doordash", "lyft",
    "airbnb", "coinbase", "robinhood", "affirm", "mercury", "modern-treasury",
    "samsara", "flexport", "faire", "benchling", "sierra", "harvey", "glean",
    "modal", "perplexityai", "huggingface", "gitlab", "asana", "twilio",
    "elastic", "atlassian", "discord", "ada", "clio", "vanta", "whatnot",
}

CANDIDATES = sorted({
    # --- Slugs confirmed by variant probing (the obvious spelling 404s) ---
    "doordashusa", "perplexity", "gleanwork", "vanta", "moderntreasury",
    # --- AI labs & applied AI ---
    "openai", "anthropic", "scaleai", "scale", "cohere", "huggingface", "cursor",
    "anysphere", "perplexityai", "perplexity", "sierra", "harvey", "glean",
    "together", "togetherai", "modal", "modallabs", "runway", "runwayml",
    "mistral", "character", "contextual", "fireworks", "fireworksai", "baseten",
    "replicate", "elevenlabs", "synthesia", "adept", "imbue", "luma", "suno",
    "stability", "cresta", "abridge", "hippocratic", "codeium", "windsurf",
    "writer", "typeface", "jasper", "copy", "tome", "mem", "rewind",
    # --- Data infrastructure & analytics ---
    "databricks", "snowflake", "fivetran", "airbyte", "dbtlabs", "getdbt",
    "prefect", "dagster", "astronomer", "confluent", "starburst", "dremio",
    "clickhouse", "timescale", "cockroachlabs", "planetscale", "neon", "supabase",
    "montecarlodata", "bigeye", "atlan", "alation", "collibra", "sigmacomputing",
    "hex", "modeanalytics", "thoughtspot", "preset", "metabase", "census",
    "hightouch", "rudderstack", "segment", "amplitude", "mixpanel", "heap",
    "posthog", "tecton", "arize", "whylabs", "galileo", "weightsandbiases",
    "wandb", "pinecone", "weaviate", "chroma", "vectara", "redis", "elastic",
    "mongodb", "datastax", "materialize", "estuary", "decodable", "tabular",
    # --- Fintech ---
    "stripe", "ramp", "brex", "plaid", "affirm", "chime", "robinhood", "coinbase",
    "gemini", "kraken", "circle", "block", "marqeta", "mercury", "modern-treasury",
    "moderntreasury", "unit", "lithic", "checkr", "alloy", "sardine", "wealthsimple",
    "koho", "borrowell", "wave", "clearco", "float", "questrade", "ratehub",
    "betterment", "wealthfront", "carta", "addepar", "pilot", "puzzle", "mercury",
    "column", "increase", "stytch", "persona", "middesk", "truv", "pave",
    # --- Marketplaces, consumer, big tech ---
    "airbnb", "doordash", "instacart", "lyft", "uber", "pinterest", "reddit",
    "snap", "spotify", "shopify", "twilio", "cloudflare", "datadog", "gitlab",
    "hashicorp", "atlassian", "asana", "notion", "linear", "figma", "canva",
    "dropbox", "box", "zoom", "docusign", "okta", "samsara", "verkada",
    "flexport", "faire", "whatnot", "discord", "roblox", "unity", "etsy",
    "wayfair", "chewy", "warbyparker", "peloton", "toast", "olo", "resy",
    "opendoor", "compass", "zillow", "redfin", "lime", "bird", "turo",
    "vimeo", "patreon", "substack", "duolingo", "grammarly", "calendly",
    "airtable", "miro", "webflow", "framer", "vercel", "netlify", "render",
    "railway", "fly", "grafana", "sentry", "launchdarkly", "postman", "sourcegraph",
    "retool", "zapier", "make", "ironclad", "rippling", "gusto", "deel",
    "justworks", "lattice", "culture-amp", "greenhouse", "ashby", "lever",
    # --- Health & bio data ---
    "benchling", "tempus", "flatiron", "komodohealth", "cedar", "oscar",
    "devoted", "includedhealth", "ro", "hims", "color", "recursion", "insitro",
    "generatebiomedicines", "verily", "veeva", "datavant", "truveta",
    # --- Toronto / Canada ---
    "ada", "clio", "vidyard", "jobber", "thinkific", "hootsuite", "coveo",
    "dialogue", "league", "ritual", "properly", "drop", "sortable", "d2l",
    "verafin", "unbounce", "loopio", "axonify", "top-hat", "tophat", "wattpad",
    "instacart", "nuvei", "lightspeed", "telus", "shopify",
    # --- Autonomy / hardware / defense-adjacent ---
    "nuro", "waymo", "zoox", "applied-intuition", "appliedintuition", "shieldai",
    "anduril", "palantir", "skydio", "zipline", "rivian", "lucidmotors",
    "matchgroup", "voleon", "twosigma", "jumptrading", "imc", "optiver", "drw",
    "hudsonrivertrading", "citadel", "point72", "balyasny", "squarepoint",
})


async def probe(fetcher: Fetcher, slug: str) -> list[tuple[str, str, int]]:
    """Return (ats, resolved_slug, job_count) for every board this slug resolves on.

    SmartRecruiters identifiers are capitalized ("Visa", not "visa"), so it is probed
    with a capitalized variant as well - a lowercase-only probe silently misses every
    board on that provider.
    """
    attempts = [
        (greenhouse.ATS, slug, greenhouse.fetch(fetcher, slug)),
        (ashby.ATS, slug, ashby.fetch(fetcher, slug)),
        (lever.ATS, slug, lever.fetch(fetcher, slug)),
        (smartrecruiters.ATS, slug, smartrecruiters.fetch(fetcher, slug)),
    ]
    capitalized = slug.capitalize()
    if capitalized != slug:
        attempts.append(
            (smartrecruiters.ATS, capitalized, smartrecruiters.fetch(fetcher, capitalized))
        )

    results = await asyncio.gather(*(a[2] for a in attempts), return_exceptions=True)

    found: list[tuple[str, str, int]] = []
    seen_ats: set[str] = set()
    for (ats, resolved, _), jobs in zip(attempts, results):
        if isinstance(jobs, Exception) or not jobs or ats in seen_ats:
            continue
        seen_ats.add(ats)
        found.append((ats, resolved, len(jobs)))
    return found


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(REPO / "config" / "companies.yaml"))
    parser.add_argument("--min-jobs", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=24)
    args = parser.parse_args()

    print(f"Probing {len(CANDIDATES)} candidate slugs across 4 ATS providers...")
    async with Fetcher(concurrency=args.concurrency, timeout=30.0) as fetcher:
        batches = await asyncio.gather(*(probe(fetcher, s) for s in CANDIDATES))

    by_ats: dict[str, dict[str, list[str]]] = {}
    total = 0
    for batch in batches:
        for ats, slug, count in batch:
            if count < args.min_jobs:
                continue
            tier = "tier1" if slug in TIER1 else "tier2"
            by_ats.setdefault(ats, {"tier1": [], "tier2": []})[tier].append(slug)
            total += 1

    lines = [
        "# Generated by scripts/validate_companies.py - do not hand-edit.",
        "# Re-run periodically: company boards migrate between ATS vendors.",
        "# tier1 polls every run; tier2 on the slower cadence in config.yaml.",
        "",
    ]
    for ats in sorted(by_ats):
        lines.append(f"{ats}:")
        for tier in ("tier1", "tier2"):
            slugs = sorted(set(by_ats[ats][tier]))
            if not slugs:
                lines.append(f"  {tier}: []")
                continue
            lines.append(f"  {tier}:")
            lines.extend(f"    - {s}" for s in slugs)
        lines.append("")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    print(f"\nResolved {total} boards across {len(by_ats)} providers -> {out}")
    for ats in sorted(by_ats):
        t1, t2 = len(set(by_ats[ats]["tier1"])), len(set(by_ats[ats]["tier2"]))
        print(f"  {ats:<16} tier1={t1:<4} tier2={t2}")


if __name__ == "__main__":
    asyncio.run(main())
