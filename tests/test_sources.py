"""Adapter parsing tests plus the live regression that protects the Ashby fetch."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from jobradar.sources import (
    amazon,
    ashby,
    greenhouse,
    jobright,
    lever,
    oraclehcm,
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


def test_workday_recovers_location_from_path():
    """Regression: Workday collapses multi-location postings to "2 Locations".

    Roles in Lima and Bangalore reached the alerts because that placeholder told the
    location filter nothing. The real place is in externalPath.
    """
    payload = {"jobPostings": [{
        "title": "Data Engineering",
        "externalPath": "/job/Lima-Peru/Data-Engineering_R-65103",
        "locationsText": "2 Locations",
        "postedOn": "Posted 3 Days Ago",
    }]}
    job = workday.parse("kyndryl.wd5/kyndryl/Careers", payload)[0]
    assert "Lima" in job.location_raw and "Peru" in job.location_raw

    from jobradar.filters import classify_location
    assert classify_location(job.location_raw) == "reject"


def test_workday_keeps_real_location_text():
    payload = {"jobPostings": [{
        "title": "AI Engineer",
        "externalPath": "/job/TORONTO-Ontario-Canada/AI-Engineer_R-123",
        "locationsText": "TORONTO, Ontario, Canada",
        "postedOn": "Posted Today",
    }]}
    job = workday.parse("rbc.wd3/rbc/rbcglobal1", payload)[0]
    from jobradar.filters import classify_location
    assert classify_location(job.location_raw) == "tier1"


def test_jobright_parses_table_and_continuation():
    """A continuation row inherits the company named in the row above it."""
    md = """
| Company | Job Title | Location | Work Model | Date Posted |
| ----- | --------- | --------- | ---- | ------- |
| **[TikTok](https://www.tiktok.com)** | **[Data Analyst Graduate](https://jobright.ai/jobs/info/abc123?utm_campaign=x)** | Fontana, CA, United States | On Site | Aug 18 |
| ↳ | **[Data Engineer, Ads](https://jobright.ai/jobs/info/def456?utm_campaign=x)** | San Jose, CA, United States | Hybrid | Aug 17 |
| **[Ramp](https://ramp.com)** | **[Analytics Engineer](https://jobright.ai/jobs/info/ghi789)** | New York, NY | On Site | Aug 16 |
"""
    jobs = jobright.parse(md)
    assert [j.company for j in jobs] == ["TikTok", "TikTok", "Ramp"]
    assert jobs[1].title == "Data Engineer, Ads"
    assert jobs[0].external_id == "abc123", "id should come from the listing path"
    assert jobs[0].location_raw == "Fontana, CA, United States"


def test_jobright_dates_roll_back_a_year_when_ahead():
    """The feed omits the year, so a date in the future belongs to last year."""
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert jobright.parse_posted("Jan 3", now).year == 2026
    assert jobright.parse_posted("Dec 20", now).year == 2025
    assert jobright.parse_posted("nonsense", now) is None


def test_jobright_dates_are_untrusted():
    md = ("| **[Acme](https://a.com)** | **[Data Engineer](https://jobright.ai/jobs/info/z1)** "
          "| New York, NY | On Site | Aug 18 |")
    assert jobright.parse(md)[0].date_trusted is False


@pytest.mark.parametrize("text,expected", [
    ("May 21, 2026", (2026, 5, 21)),
    ("December 1, 2025", (2025, 12, 1)),
])
def test_amazon_posted_date_parsing(text, expected):
    parsed = amazon.parse_posted(text)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == expected


@pytest.mark.parametrize("text", [None, "", "not a date", "21 May"])
def test_amazon_unparseable_dates(text):
    assert amazon.parse_posted(text) is None


def test_amazon_parse_folds_qualifications_into_description():
    """The years requirement lives in the qualifications, not the description."""
    payload = {"jobs": [{
        "title": "Data Engineer I",
        "job_path": "/en/jobs/123/data-engineer-i",
        "location": "Seattle, WA",
        "posted_date": "May 21, 2026",
        "description": "Build pipelines.",
        "basic_qualifications": "- 1+ years of data engineering experience<br/>- SQL",
    }]}
    job = amazon.parse(payload)[0]
    assert job.url == "https://www.amazon.jobs/en/jobs/123/data-engineer-i"
    assert "1+ years" in job.description and "Build pipelines." in job.description
    from jobradar.filters import min_years_required
    assert min_years_required(job.description) == 1


def test_oraclehcm_slug_and_parse():
    assert oraclehcm.split_slug("jpmc.fa.oraclecloud.com/CX_1001") == (
        "jpmc.fa.oraclecloud.com", "CX_1001")
    for bad in ("jpmc.fa.oraclecloud.com", "a/b/c", ""):
        assert oraclehcm.split_slug(bad) is None

    payload = {"items": [{"requisitionList": [{
        "Id": 210766963,
        "Title": "Data Engineer",
        "PostedDate": "2026-08-04",
        "PrimaryLocation": "New York, NY, United States",
        "ShortDescriptionStr": "Own our pipelines.",
    }]}]}
    job = oraclehcm.parse("jpmc.fa.oraclecloud.com/CX_1001", payload)[0]
    assert job.company == "jpmc"
    assert job.url.endswith("/sites/CX_1001/job/210766963")
    assert job.posted_at is not None and job.date_trusted


def test_new_adapters_tolerate_garbage():
    assert amazon.parse({}) == []
    assert oraclehcm.parse("bad", {"items": [{"requisitionList": [{"Id": 1}]}]}) == []
    assert jobright.parse("") == []
    assert jobright.parse("| not | a | real | table |") == []
