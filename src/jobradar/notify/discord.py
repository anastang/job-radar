"""Discord webhook notifier.

Alerts are the product, so the embed is built to be actionable at a glance on a
phone lock screen: role and company first, then how fresh it is, then the stack
overlap that explains why it matched.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Iterable, Sequence

import httpx

from ..models import Job
from ..scoring import Score

log = logging.getLogger(__name__)

MAX_EMBEDS_PER_MESSAGE = 10
COLOR_PRIORITY = 0xE67E22  # orange - drop everything
COLOR_NORMAL = 0x3498DB    # blue - worth a look

_ACRONYMS = {"ai", "ml", "bi", "hq", "gm", "io"}


def pretty_company(slug: str) -> str:
    cleaned = slug.replace("-", " ").replace("_", " ").strip()
    if not cleaned:
        return slug
    if " " not in cleaned and not cleaned.islower():
        return cleaned  # already branded, e.g. "OpenAI"
    return " ".join(
        word.upper() if word.lower() in _ACRONYMS else word.capitalize()
        for word in cleaned.split()
    )


def relative_time(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    delta = (datetime.now(timezone.utc) - dt).total_seconds()
    if delta < 120:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def build_embed(job: Job, score: Score, priority: bool) -> dict:
    location = job.location_raw or ("Remote" if job.is_remote else "Location not listed")
    fields = [
        {"name": "Match", "value": f"**{score.total:.0f}**/100", "inline": True},
        {"name": "Posted", "value": relative_time(job.best_date), "inline": True},
        {"name": "Source", "value": job.ats, "inline": True},
    ]
    if score.matched_skills:
        overlap = ", ".join(score.matched_skills[:14])
        fields.append({"name": "Stack overlap", "value": overlap[:1024], "inline": False})
    if job.salary_min or job.salary_max:
        currency = job.salary_currency or "USD"
        lo = f"{job.salary_min:,.0f}" if job.salary_min else "?"
        hi = f"{job.salary_max:,.0f}" if job.salary_max else "?"
        fields.append({"name": "Comp", "value": f"{lo}-{hi} {currency}", "inline": True})

    return {
        "title": job.title[:256],
        "url": job.apply_link or None,
        "color": COLOR_PRIORITY if priority else COLOR_NORMAL,
        "description": f"**{pretty_company(job.company)}** · {location}"[:4096],
        "fields": fields,
        "footer": {"text": score.breakdown()},
        "timestamp": job.best_date.isoformat() if job.best_date else None,
    }


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> bool:
    for attempt in range(4):
        try:
            resp = await client.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning("Discord post failed (%s), retrying", exc)
            await asyncio.sleep(2 * (attempt + 1))
            continue

        if resp.status_code == 429:
            try:
                wait = float(resp.json().get("retry_after", 2))
            except Exception:
                wait = 2.0
            log.info("Discord rate limited; waiting %.1fs", wait)
            await asyncio.sleep(min(wait, 30) + 0.25)
            continue

        if resp.status_code >= 400:
            log.error("Discord returned %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    return False


async def send_jobs(
    webhook_url: str,
    items: Sequence[tuple[Job, Score]],
    *,
    priority_min: float = 75.0,
    mention: str = "",
) -> int:
    """Post every match, newest/highest first. Returns the count delivered."""
    if not items:
        return 0

    ranked = sorted(items, key=lambda pair: pair[1].total, reverse=True)
    delivered = 0

    async with httpx.AsyncClient(timeout=20.0) as client:
        for start in range(0, len(ranked), MAX_EMBEDS_PER_MESSAGE):
            chunk = ranked[start: start + MAX_EMBEDS_PER_MESSAGE]
            embeds = [
                build_embed(job, score, score.total >= priority_min)
                for job, score in chunk
            ]
            top = chunk[0][1].total
            header = ""
            if start == 0:
                count = len(ranked)
                noun = "role" if count == 1 else "roles"
                header = f"**{count} new {noun}** matching your profile"
                if top >= priority_min:
                    header = f"{mention + ' ' if mention else ''}🔥 {header}".strip()

            payload: dict = {"embeds": embeds}
            if header:
                payload["content"] = header[:2000]
            payload["allowed_mentions"] = {"parse": ["users"] if mention else []}

            if await _post(client, webhook_url, payload):
                delivered += len(chunk)
            # Stay clear of the webhook burst limit between batches.
            if start + MAX_EMBEDS_PER_MESSAGE < len(ranked):
                await asyncio.sleep(1.0)

    return delivered


async def send_text(webhook_url: str, text: str) -> bool:
    async with httpx.AsyncClient(timeout=20.0) as client:
        return await _post(client, webhook_url, {"content": text[:2000]})


def _sample() -> tuple[Job, Score]:
    job = Job(
        ats="greenhouse",
        company="linear",
        external_id="test-1",
        title="Analytics Engineer",
        url="https://example.com/apply",
        location_raw="New York, NY; San Francisco, CA",
        posted_at=datetime.now(timezone.utc),
        salary_min=130000,
        salary_max=165000,
        salary_currency="USD",
    )
    score = Score(
        total=88.0, family=38.0, skills=31.0, location=15.0, early=4.0, freshness=4.0,
        matched_skills=["dbt", "Airflow", "SQL", "Python", "Snowflake", "Databricks"],
    )
    return job, score


if __name__ == "__main__":  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="Send a test Discord alert")
    parser.add_argument("--test", action="store_true", help="post a sample embed")
    parser.add_argument("--webhook", default=os.environ.get("DISCORD_WEBHOOK_URL", ""))
    args = parser.parse_args()

    if not args.webhook:
        raise SystemExit("Set DISCORD_WEBHOOK_URL or pass --webhook")
    if args.test:
        sent = asyncio.run(send_jobs(args.webhook, [_sample()], priority_min=75.0))
        print(f"Delivered {sent} embed(s)")
