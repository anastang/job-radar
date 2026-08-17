"""Orchestrator: fetch -> diff -> gate -> score -> notify -> persist.

The diff against saved state is what makes this an alerting tool rather than a
search tool: only postings never seen before are ever considered for notification.

Cold-start safety: with no prior state, every posting looks new and would fire
hundreds of alerts. The first run therefore bootstraps silently unless explicitly
told otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from .filters import Verdict, evaluate
from .models import Job
from .notify import discord
from .scoring import Profile, Score, score_job
from .sources import ATS_ADAPTERS, Fetcher, simplify
from .store import State

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger("jobradar")


# ------------------------------------------------------------------ config

def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_companies(primary: Path) -> dict[str, Any]:
    """Merge every ``companies*.yaml`` in the config directory.

    validate_companies.py and discover_yc.py each regenerate their own file, so
    they are kept separate and merged here - otherwise re-running either generator
    would silently wipe out the other's boards.
    """
    merged: dict[str, dict[str, list[str]]] = {}
    paths = sorted(primary.parent.glob("companies*.yaml")) if primary.parent.is_dir() else []
    if primary.exists() and primary not in paths:
        paths.append(primary)

    for path in paths:
        for ats, tiers in (load_yaml(path) or {}).items():
            if not isinstance(tiers, dict):
                continue
            bucket = merged.setdefault(ats, {"tier1": [], "tier2": []})
            for tier in ("tier1", "tier2"):
                bucket[tier].extend(tiers.get(tier) or [])

    # A slug promoted to tier1 in one file must not also be polled as tier2.
    for tiers in merged.values():
        tier1 = list(dict.fromkeys(tiers["tier1"]))
        tier2 = [s for s in dict.fromkeys(tiers["tier2"]) if s not in set(tier1)]
        tiers["tier1"], tiers["tier2"] = tier1, tier2
    return merged


def select_targets(companies: dict[str, Any], include_tier2: bool) -> list[tuple[str, str]]:
    """Flatten the merged company config into (ats, slug) pairs for this run."""
    targets: list[tuple[str, str]] = []
    for ats, tiers in (companies or {}).items():
        if ats not in ATS_ADAPTERS or not isinstance(tiers, dict):
            continue
        wanted = ["tier1"] + (["tier2"] if include_tier2 else [])
        for tier in wanted:
            for slug in tiers.get(tier) or []:
                targets.append((ats, slug))
    return targets


def due(state: State, key: str, minutes: int, force: bool = False) -> bool:
    if force or minutes <= 0:
        return True
    raw = state.meta.get(key)
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last >= timedelta(minutes=minutes)


# ------------------------------------------------------------------- fetch

async def collect(
    fetcher: Fetcher,
    targets: list[tuple[str, str]],
    include_simplify: bool,
) -> list[Job]:
    tasks = [ATS_ADAPTERS[ats](fetcher, slug) for ats, slug in targets]
    if include_simplify:
        tasks.append(simplify.fetch(fetcher))

    jobs: list[Job] = []
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, BaseException):
            log.debug("source failed: %s", result)
            continue
        jobs.extend(result)
    return jobs


# ---------------------------------------------------------------- pipeline

def load_startup_slugs(config_dir: Path) -> frozenset[str]:
    """Slugs discovered from the YC directory, used to boost small companies.

    Membership in companies_yc.yaml is the signal - everything in that file is an
    actively-hiring YC company, which is a far better proxy for "small startup" than
    anything guessable from a job posting.
    """
    path = config_dir / "companies_yc.yaml"
    slugs: set[str] = set()
    for tiers in (load_yaml(path) or {}).values():
        if not isinstance(tiers, dict):
            continue
        for tier in ("tier1", "tier2"):
            slugs.update(s.lower() for s in (tiers.get(tier) or []))
    return frozenset(slugs)


def process(
    jobs: Iterable[Job],
    state: State,
    profile: Profile,
    filter_cfg: dict[str, Any],
    notify_min: float,
    startup_slugs: frozenset[str] | None = None,
) -> tuple[list[tuple[Job, Score]], dict[str, int]]:
    """Gate and score only postings we have never seen. Returns (matches, stats)."""
    matches: list[tuple[Job, Score]] = []
    stats: dict[str, int] = {"total": 0, "new": 0, "passed": 0, "notify": 0}
    rejections: dict[str, int] = {}

    for job in jobs:
        stats["total"] += 1
        if not state.is_new(job):
            continue
        stats["new"] += 1

        verdict: Verdict = evaluate(job, filter_cfg)
        if not verdict.passed:
            rejections[verdict.reason] = rejections.get(verdict.reason, 0) + 1
            state.mark_seen(job)
            continue

        stats["passed"] += 1
        score = score_job(job, verdict, profile, startup_slugs=startup_slugs)
        if score.total >= notify_min:
            stats["notify"] += 1
            matches.append((job, score))
        else:
            state.mark_seen(job, score=score.total)

    stats.update({f"reject_{k}": v for k, v in rejections.items()})
    return matches, stats


def render_table(matches: list[tuple[Job, Score]], limit: int = 40) -> str:
    if not matches:
        return "  (no matches)"
    rows = []
    for job, score in sorted(matches, key=lambda p: p[1].total, reverse=True)[:limit]:
        age = job.age_hours
        age_s = f"{age / 24:.0f}d" if age and age >= 24 else (f"{age:.0f}h" if age else "?")
        rows.append(
            f"  [{score.total:5.1f}] {age_s:>4}  {job.company[:16]:<16} "
            f"{job.title[:46]:<46} {(job.location_raw or '-')[:30]}"
        )
    return "\n".join(rows)


# -------------------------------------------------------------------- main

async def run(args: argparse.Namespace) -> int:
    cfg = load_yaml(Path(args.config))
    companies = load_companies(Path(args.companies))
    profile = Profile.load(args.profile)

    fetch_cfg = cfg.get("fetch") or {}
    filter_cfg = cfg.get("filters") or {}
    score_cfg = cfg.get("scoring") or {}
    poll_cfg = cfg.get("polling") or {}
    state_cfg = cfg.get("state") or {}

    notify_min = float(args.min_score if args.min_score is not None
                       else score_cfg.get("notify_min", 55))
    priority_min = float(score_cfg.get("priority_min", 75))

    state_path = Path(args.state or state_cfg.get("path", "state/seen.json"))
    if not state_path.is_absolute():
        state_path = REPO / state_path
    state = State(state_path).load()

    cold_start = len(state) == 0
    include_tier2 = due(state, "last_tier2", int(poll_cfg.get("tier2_every_minutes", 30)),
                        force=args.all_tiers or cold_start)
    include_simplify = due(state, "last_simplify",
                           int(poll_cfg.get("simplify_every_minutes", 15)),
                           force=args.all_tiers or cold_start)

    targets = select_targets(companies, include_tier2)
    if not targets:
        log.error("No companies configured - run scripts/validate_companies.py first")
        return 1

    log.info(
        "Polling %d boards (tier2=%s, simplify=%s); %d jobs already known",
        len(targets), include_tier2, include_simplify, len(state),
    )

    async with Fetcher(
        concurrency=int(fetch_cfg.get("concurrency", 12)),
        timeout=float(fetch_cfg.get("timeout_seconds", 25)),
        retries=int(fetch_cfg.get("retries", 3)),
        etags=state.etags,
    ) as fetcher:
        jobs = await collect(fetcher, targets, include_simplify)
        http = dict(fetcher.stats)

    log.info("Fetched %d postings (%s)", len(jobs), http)

    if args.since:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.since)
        jobs = [j for j in jobs if j.best_date and j.best_date >= cutoff]
        log.info("Filtered to %d postings from the last %d days", len(jobs), args.since)

    # Cold start: record the existing world without alerting on all of it.
    if cold_start and not args.notify_on_cold_start and not args.dry_run:
        recorded = state.mark_all_seen(jobs)
        state.meta.update({
            "last_tier2": datetime.now(timezone.utc).isoformat(),
            "last_simplify": datetime.now(timezone.utc).isoformat(),
            "bootstrapped_at": datetime.now(timezone.utc).isoformat(),
        })
        state.save()
        print(
            f"\nBootstrapped: recorded {recorded} existing postings without alerting.\n"
            f"Future runs will only notify on postings that appear from now on."
        )
        return 0

    startup_slugs = load_startup_slugs(Path(args.companies).parent)
    matches, stats = process(
        jobs, state, profile, filter_cfg, notify_min, startup_slugs=startup_slugs
    )

    log.info(
        "New=%d passed=%d notify=%d", stats["new"], stats["passed"], stats["notify"]
    )
    rejects = {k[7:]: v for k, v in stats.items() if k.startswith("reject_")}
    if rejects:
        top = sorted(rejects.items(), key=lambda kv: -kv[1])[:8]
        log.info("Top rejections: %s", ", ".join(f"{k}={v}" for k, v in top))

    if args.dry_run:
        print(f"\n{len(matches)} match(es) at threshold {notify_min}:\n")
        print(render_table(matches, limit=args.limit))
        return 0

    if args.bootstrap:
        recorded = state.mark_all_seen(jobs)
        state.save()
        print(f"Bootstrapped {recorded} postings without alerting.")
        return 0

    sent = 0
    if matches:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        if not webhook:
            log.error("DISCORD_WEBHOOK_URL is not set - not sending %d match(es)", len(matches))
        else:
            sent = await discord.send_jobs(
                webhook,
                matches,
                priority_min=priority_min,
                mention=str((cfg.get("notify") or {}).get("mention", "")),
            )
            log.info("Delivered %d alert(s)", sent)

    for job, score in matches:
        state.mark_seen(job, score=score.total, notified=sent > 0)

    now = datetime.now(timezone.utc).isoformat()
    if include_tier2:
        state.meta["last_tier2"] = now
    if include_simplify:
        state.meta["last_simplify"] = now
    state.meta["last_run"] = now

    pruned = state.prune(int(state_cfg.get("prune_days", 90)))
    if pruned:
        log.info("Pruned %d stale state entries", pruned)
    state.save()

    print(f"{stats['new']} new / {stats['passed']} passed gates / {sent} alerted")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobradar", description="Early-career data/AI job radar")
    p.add_argument("--config", default=str(REPO / "config" / "config.yaml"))
    p.add_argument("--companies", default=str(REPO / "config" / "companies.yaml"))
    p.add_argument("--profile", default=str(REPO / "config" / "profile.yaml"))
    p.add_argument("--state", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="print matches; never notify or write state")
    p.add_argument("--bootstrap", action="store_true",
                   help="record everything as seen without alerting")
    p.add_argument("--notify-on-cold-start", action="store_true",
                   help="alert on the very first run instead of bootstrapping")
    p.add_argument("--all-tiers", action="store_true",
                   help="poll tier2 and the community feed regardless of cadence")
    p.add_argument("--since", type=int, default=None,
                   help="only consider postings from the last N days")
    p.add_argument("--min-score", type=float, default=None)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    # httpx logs a line per request; at ~170 boards that buries our own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
