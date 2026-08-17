"""Adapter parsing tests plus the live regression that protects the Ashby fetch."""

from __future__ import annotations

import asyncio

import pytest

from jobradar.sources import ashby, greenhouse, lever, simplify
from jobradar.sources.base import DEFAULT_HEADERS, Fetcher


def test_default_headers_carry_a_browser_user_agent():
    """Ashby 404s on every request without one. Silent, total, easy to reintroduce."""
    ua = DEFAULT_HEADERS.get("User-Agent", "")
    assert "Mozilla" in ua and "Chrome" in ua


def test_greenhouse_parse():
    payload = {"jobs": [{
        "id": 8077887,
        "title": "Data Engineer ",
        "absolute_url": "https://stripe.com/jobs/search?gh_jid=8077887",
        "location": {"name": "SF, NYC, SEA, CHI"},
        "first_published": "2026-07-22T13:15:53-04:00",
        "updated_at": "2026-08-06T12:10:17-04:00",
        "content": "&lt;p&gt;Build pipelines with dbt&lt;/p&gt;",
        "departments": [{"name": "Data"}],
    }]}
    job = greenhouse.parse("stripe", payload)[0]
    assert job.title == "Data Engineer"          # trimmed
    assert job.location_raw == "SF, NYC, SEA, CHI"
    assert "dbt" in job.description              # unescaped and de-tagged
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.department == "Data"
    assert job.key


def test_ashby_parse_and_unlisted_skipped():
    payload = {"jobs": [
        {
            "id": "abc-123",
            "title": "Analytics Engineer",
            "location": "New York, NY (HQ)",
            "secondaryLocations": [{"location": "Remote (Canada)"}],
            "publishedAt": "2026-04-07T17:12:35.753+00:00",
            "isListed": True,
            "isRemote": True,
            "workplaceType": "Hybrid",
            "jobUrl": "https://jobs.ashbyhq.com/ramp/abc-123",
            "applyUrl": "https://jobs.ashbyhq.com/ramp/abc-123/application",
            "descriptionPlain": "Own the dbt project.",
            "employmentType": "FullTime",
        },
        {"id": "hidden", "title": "Secret Role", "isListed": False},
    ]}
    jobs = ashby.parse("ramp", payload)
    assert len(jobs) == 1, "unlisted postings must be skipped"
    job = jobs[0]
    assert "New York" in job.location_raw and "Remote (Canada)" in job.location_raw
    assert job.apply_link.endswith("/application")
    assert job.is_remote is True


def test_lever_parse_epoch_and_salary():
    payload = [{
        "id": "3414ba28",
        "text": "Data Analyst",
        "categories": {"location": "New York, New York", "commitment": "Full-time",
                       "department": "Analytics"},
        "createdAt": 1779223091267,
        "descriptionPlain": "Analyze things.",
        "additionalPlain": "Benefits.",
        "salaryRange": {"min": 150000, "max": 180000, "currency": "USD"},
        "hostedUrl": "https://jobs.lever.co/x/3414ba28",
        "applyUrl": "https://jobs.lever.co/x/3414ba28/apply",
        "workplaceType": "hybrid",
    }]
    job = lever.parse("x", payload)[0]
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.salary_min == 150000 and job.salary_currency == "USD"
    assert "Analyze things." in job.description and "Benefits." in job.description


def test_simplify_parse_skips_inactive():
    payload = [
        {"id": "a", "title": "Data Engineer", "company_name": "Acme", "active": True,
         "is_visible": True, "date_posted": 1767841111, "locations": ["SF", "NYC"],
         "url": "https://example.com/a", "sponsorship": "Other"},
        {"id": "b", "title": "Old Role", "company_name": "Acme", "active": False,
         "is_visible": True, "date_posted": 1767841111, "locations": ["SF"],
         "url": "https://example.com/b"},
        {"id": "c", "title": "Cleared Role", "company_name": "Acme", "active": True,
         "is_visible": True, "date_posted": 1767841111, "locations": ["DC"],
         "url": "https://example.com/c",
         "sponsorship": "U.S. Citizenship is Required"},
    ]
    jobs = simplify.parse(payload)
    assert [j.external_id for j in jobs] == ["a", "c"]
    assert jobs[0].location_raw == "SF; NYC"
    assert jobs[1].extra["blocking_sponsorship"] is True


def test_adapters_tolerate_garbage_payloads():
    assert greenhouse.parse("x", {}) == []
    assert ashby.parse("x", {"jobs": None}) == []
    assert lever.parse("x", []) == []
    assert simplify.parse([]) == []


@pytest.mark.live
def test_ashby_live_returns_jobs():
    """Guards the User-Agent requirement against real infrastructure."""
    async def go():
        async with Fetcher(concurrency=4, timeout=30) as f:
            return await ashby.fetch(f, "ramp")

    jobs = asyncio.run(go())
    assert len(jobs) > 0, "Ashby returned nothing - User-Agent header likely dropped"
    assert all(j.key for j in jobs)


@pytest.mark.live
def test_greenhouse_live_returns_jobs():
    async def go():
        async with Fetcher(concurrency=4, timeout=30) as f:
            return await greenhouse.fetch(f, "stripe")

    jobs = asyncio.run(go())
    assert len(jobs) > 0
    assert any(j.posted_at for j in jobs), "first_published should populate posted_at"
