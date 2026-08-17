"""Gate behaviour tests, anchored to postings observed in live runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobradar.filters import (
    classify_location,
    classify_role,
    evaluate,
    min_years_required,
)
from jobradar.models import Job, parse_dt, strip_html


def make_job(title: str, location: str = "New York, NY", description: str = "",
             company: str = "acme", **kw) -> Job:
    return Job(
        ats="greenhouse",
        company=company,
        external_id="1",
        title=title,
        url="https://example.com",
        location_raw=location,
        description=description,
        posted_at=datetime.now(timezone.utc) - timedelta(hours=2),
        **kw,
    )


# --------------------------------------------------------------- role families

@pytest.mark.parametrize("title,family", [
    ("Data Engineer", "data_engineer"),
    ("Analytics Engineer", "analytics_engineer"),
    ("Data Analyst, Go-To-Market Sales Insights", "data_analyst"),
    ("AI Engineer", "ai_engineer"),
    ("Business Intelligence Analyst - DealCloud", "data_analyst"),
    ("Forward Deployed Engineer", "forward_deployed"),
    ("Software Engineer, Data Infrastructure", "data_engineer"),
])
def test_role_families_match(title, family):
    result = classify_role(title)
    assert result is not None, f"{title!r} should classify"
    assert result[0] == family


@pytest.mark.parametrize("title", [
    "Data Center Technician",
    "Data Entry Clerk",
    "Quality Engineer - Data Science",
    "Instructional Assistant - Data Analyst",
    "Financial Analyst",
    "Security Analyst",
    "Product Manager, AI Access",
])
def test_non_roles_rejected(title):
    assert classify_role(title) is None


# ------------------------------------------------------------------- seniority

@pytest.mark.parametrize("title", [
    "Senior Data Engineer",
    "Staff Analytics Engineer",
    "Principal Data Engineer",
    "Data Engineering Manager",
    "Director, Data Science",
    "Forward Deployed Architect",
    "Data Engineer III",
])
def test_senior_titles_rejected(title):
    assert evaluate(make_job(title)).reason == "senior_title"


@pytest.mark.parametrize("title", [
    "Principal Software Engineer, Data",   # caught by role family, not seniority
    "Senior Named Account Executive",
])
def test_other_senior_titles_still_rejected(title):
    """Reason varies by which gate fires first; what matters is that none pass."""
    assert not evaluate(make_job(title)).passed


@pytest.mark.parametrize("title", ["Data Engineer I", "Data Engineer II"])
def test_junior_levels_allowed(title):
    assert evaluate(make_job(title)).passed


def test_internships_rejected_by_default():
    assert evaluate(make_job("Data Engineer Intern")).reason == "internship"
    assert evaluate(make_job("Data Analyst Co-op")).reason == "internship"


def test_internships_allowed_when_configured():
    assert evaluate(make_job("Data Engineer Intern"), {"allow_internships": True}).passed


# --------------------------------------------------- years-of-experience parsing

@pytest.mark.parametrize("text,expected", [
    ("We need 5+ years of experience in analytics engineering", 5),
    ("Requires 3-5 years of relevant experience", 3),
    ("At least 2 years experience with SQL", 2),
    ("0-2 years of experience preferred", 0),
    # Baseline wins over the higher "preferred" bar, or we reject roles he qualifies for.
    ("2+ years of experience required. 8+ years of experience preferred.", 2),
    # Prose that mentions years but not experience must not register.
    ("Founded 20 years ago, we serve millions", None),
    ("", None),
])
def test_min_years_required(text, expected):
    assert min_years_required(text) == expected


def test_high_experience_bar_rejected():
    # The real Linear "Analytics Engineer" posting - correctly not early-career.
    job = make_job(
        "Analytics Engineer",
        location="North America",
        description="What we're looking for\n - 5+ years of experience in analytics "
                    "engineering, data analytics, or a related field",
    )
    assert evaluate(job).reason == "years_5"


def test_missing_description_is_not_a_rejection():
    """Feed sources carry no body; unknown experience must not mean rejected."""
    assert evaluate(make_job("Data Engineering New Grad", description="")).passed


# ------------------------------------------------------- work authorization (TN)

def test_export_control_boilerplate_does_not_block():
    """Regression: the real Cloudflare "Data Analyst" posting.

    Generic Export Administration Regulations language appears on many ordinary US
    postings and says nothing about citizenship. Treating it as an ITAR restriction
    silently discarded good roles.
    """
    job = make_job(
        "Data Analyst",
        location="Hybrid",
        description="This position may require access to information protected under "
                    "U.S. export control laws, including the U.S. Export "
                    "Administration Regulations.",
    )
    assert evaluate(job).passed


def test_no_sponsorship_language_does_not_block():
    """He is a Canadian citizen on TN status and answers yes to this question."""
    job = make_job(
        "Data Engineer",
        description="Applicants must be authorized to work in the US without "
                    "sponsorship. We do not sponsor employment visas.",
    )
    assert evaluate(job).passed


@pytest.mark.parametrize("description,reason", [
    ("Must hold an active TS/SCI security clearance", "auth_clearance"),
    ("U.S. citizenship is required for this role", "auth_us_citizenship"),
    ("This role is restricted to U.S. persons only under ITAR", "auth_itar"),
])
def test_genuine_auth_blockers_reject(description, reason):
    assert evaluate(make_job("Data Engineer", description=description)).reason == reason


def test_blocked_company_rejected():
    cfg = {"blocked_companies": ["johns hopkins", "mantech"]}
    job = make_job("Data Engineer", company="Johns Hopkins Applied Physics Laboratory")
    assert evaluate(job, cfg).reason == "blocked_company"


# -------------------------------------------------------------------- locations

@pytest.mark.parametrize("location,tier", [
    ("San Francisco, CA", "tier1"),
    ("New York, NY (HQ)", "tier1"),
    ("Toronto, ON", "tier1"),
    ("Brampton, ON, Canada", "tier1"),
    ("SF, NYC, SEA, CHI", "tier1"),
    # A multi-location posting that includes a target city must not be rejected.
    ("New York City, NY; London", "tier1"),
    ("Remote US", "tier2"),
    ("North America", "tier2"),
    ("Austin, TX, United States", "tier2"),
    ("Hybrid", "unknown"),
    ("Dublin, Ireland", "reject"),
    ("Paris, France", "reject"),
    ("Sydney, Australia", "reject"),
    ("São Paulo", "reject"),
])
def test_location_classification(location, tier):
    assert classify_location(location) == tier


def test_non_north_america_rejected():
    assert evaluate(make_job("Data Engineer", "Dublin, Ireland")).reason == "location"


def test_london_ontario_not_treated_as_uk():
    """"London" is ambiguous; the province marker has to win."""
    assert classify_location("London, ON") != "reject"


# ---------------------------------------------------------------- model helpers

def test_parse_dt_handles_every_source_format():
    assert parse_dt("2026-07-22T13:15:53-04:00") is not None      # Greenhouse
    assert parse_dt("2026-04-07T17:12:35.753+00:00") is not None  # Ashby
    assert parse_dt(1779223091267).year == 2026                   # Lever epoch ms
    assert parse_dt(1767841111).year == 2026                      # Simplify epoch s
    assert parse_dt(None) is None
    assert parse_dt("") is None


def test_strip_html_unescapes_before_stripping():
    """Greenhouse entity-encodes its content; tags must not survive as literal text."""
    assert strip_html("&lt;p&gt;Build &amp; ship pipelines&lt;/p&gt;") == "Build & ship pipelines"


def test_job_key_is_stable_and_distinct():
    a = make_job("Data Engineer")
    b = make_job("Data Engineer (renamed)")   # same ats/company/id
    assert a.key == b.key, "key must not depend on mutable fields like title"

    c = Job(ats="greenhouse", company="acme", external_id="2",
            title="Data Engineer", url="")
    assert a.key != c.key
