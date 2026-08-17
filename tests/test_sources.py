"""Adapter parsing tests plus the live regression that protects the Ashby fetch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from jobradar.sources import (
    ashby,
    greenhouse,
    lever,
    simplify,
    workable,
    workday,
)
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


def test_workable_parse():
    payload = {"jobs": [{
        "title": "AI & ML Engineer",
        "shortcode": "960601AF0E",
        "url": "https://apply.workable.com/j/960601AF0E",
        "application_url": "https://apply.workable.com/j/960601AF0E/apply",
        "published_on": "2026-07-08",
        "city": "London", "state": "England", "country": "United Kingdom",
        "telecommuting": True,
        "department": "Engineering",
        "description": "<p>Build <strong>dbt</strong> models</p>",
    }]}
    job = workable.parse("acme", payload)[0]
    assert job.external_id == "960601AF0E"
    assert job.description == "Build dbt models"
    assert job.is_remote is True
    assert "London" in job.location_raw and "Remote" in job.location_raw
    assert job.posted_at is not None and job.posted_at.year == 2026


@pytest.mark.parametrize("text,days", [
    ("Posted Today", 0),
    ("Just Posted", 0),
    ("Posted Yesterday", 1),
    ("Posted 13 Days Ago", 13),
])
def test_workday_posted_on_parsing(text, days):
    parsed = workday.parse_posted_on(text)
    assert parsed is not None
    age = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    assert abs(age - days) < 0.1


def test_workday_thirty_plus_days_counts_as_stale():
    """"30+ Days Ago" means at least 30 - it must not sneak under a 30-day gate."""
    parsed = workday.parse_posted_on("Posted 30+ Days Ago")
    assert parsed is not None
    age_days = (datetime.now(timezone.utc) - parsed).total_seconds() / 86400
    assert age_days > 30


@pytest.mark.parametrize("text", [None, "", "garbage", "Posted sometime"])
def test_workday_unparseable_dates_return_none(text):
    assert workday.parse_posted_on(text) is None


def test_workday_slug_splitting():
    assert workday.split_slug("nvidia.wd5/nvidia/NVIDIAExternalCareerSite") == (
        "nvidia.wd5", "nvidia", "NVIDIAExternalCareerSite")
    for bad in ("nvidia", "a/b", "a/b/c/d", ""):
        assert workday.split_slug(bad) is None


def test_workday_parse_builds_viewable_url():
    payload = {"jobPostings": [{
        "title": "Data Engineer",
        "externalPath": "/job/US-CA-Santa-Clara/Data-Engineer_JR123",
        "locationsText": "US, CA, Santa Clara",
        "postedOn": "Posted 3 Days Ago",
    }]}
    job = workday.parse("nvidia.wd5/nvidia/NVIDIAExternalCareerSite", payload)[0]
    assert job.company == "nvidia"
    assert job.url == (
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        "/job/US-CA-Santa-Clara/Data-Engineer_JR123")
    assert job.posted_at is not None


def test_adapters_tolerate_garbage_payloads():
    assert greenhouse.parse("x", {}) == []
    assert ashby.parse("x", {"jobs": None}) == []
    assert lever.parse("x", []) == []
    assert simplify.parse([]) == []
    assert workable.parse("x", {}) == []
    assert workday.parse("bad-slug", {"jobPostings": [{"externalPath": "/j"}]}) == []


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
