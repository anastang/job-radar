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


def test_stale_postings_rejected_when_configured():
    """Being early is the point; a month-old posting is not worth an alert."""
    old = Job(ats="greenhouse", company="acme", external_id="1", title="Data Engineer",
              url="https://example.com", location_raw="New York, NY",
              posted_at=datetime.now(timezone.utc) - timedelta(days=45))
    assert evaluate(old, {"max_age_days": 30}).reason == "stale"
    # Without the setting configured, age is not a gate at all.
    assert evaluate(old, {}).passed


def test_recent_posting_survives_age_gate():
    recent = make_job("Data Engineer")
    assert evaluate(recent, {"max_age_days": 30}).passed


def test_feed_dates_are_not_used_to_reject_as_stale():
    """Regression: the real EXL "Data Engineer" listing.

    Simplify reported date_posted 2026-07-08 while the employer's own page said
    2026-08-16 - the feed records when *it* indexed a job and never refreshes that
    when the employer re-posts. Gating on it would discard a day-old role as stale,
    which is the exact opposite of what this tool is for.
    """
    feed_job = Job(
        ats="simplify", company="EXL", external_id="1", title="Data Engineer",
        url="https://example.com", location_raw="United States",
        posted_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    assert not feed_job.date_trusted
    assert evaluate(feed_job, {"max_age_days": 30}).passed

    # An employer-sourced date of the same age must still be rejected.
    ats_job = Job(
        ats="greenhouse", company="acme", external_id="1", title="Data Engineer",
        url="https://example.com", location_raw="United States",
        posted_at=datetime.now(timezone.utc) - timedelta(days=40),
    )
    assert ats_job.date_trusted
    assert evaluate(ats_job, {"max_age_days": 30}).reason == "stale"


def test_undated_posting_not_rejected_as_stale():
    """Some sources omit dates; unknown age must not mean rejected."""
    undated = Job(ats="simplify", company="acme", external_id="1",
                  title="Data Engineer", url="https://example.com",
                  location_raw="Toronto, ON", posted_at=None)
    assert evaluate(undated, {"max_age_days": 30}).passed


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
    ("", None),
])
def test_min_years_required(text, expected):
    assert min_years_required(text) == expected


@pytest.mark.parametrize("text,expected", [
    # Regression: real postings that slipped past the gate entirely. None of these
    # contain the word "experience", so requiring it returned None and a role
    # wanting four to eight years scored 88.
    ("4-8+ years in data or analytics engineering, you have built pipelines", 4),
    ("2-5+ years working as a data engineer or applied data scientist", 2),
    ("3+ years in an analytics role (Data Analyst, BI Analyst)", 3),
    ("5+ years building production ML systems", 5),
])
def test_years_parsed_without_the_word_experience(text, expected):
    assert min_years_required(text) == expected


@pytest.mark.parametrize("text", [
    "Founded 20 years ago, we serve millions",
    "Celebrating 15 years in business",
    "over the past 10 years we have grown",
    "Our 30 year history of innovation",
])
def test_company_prose_is_not_a_requirement(text):
    assert min_years_required(text) is None


@pytest.mark.parametrize("text", [
    # Regression: Linear's real posting. Counting tenure perks made a role wanting
    # 5+ years look like a 2-year one, because the minimum wins.
    "Paid month off after 4 years & every 2 years thereafter",
    "Sabbatical after 5 years",
    "Unlimited vacation and 3 years of vesting",
    "401(k) matching after 1 year of service",
])
def test_benefits_and_tenure_are_not_requirements(text):
    assert min_years_required(text) is None


@pytest.mark.parametrize("text,expected", [
    # Requirement language follows the number, benefits language precedes it - so a
    # real requirement still counts even when perks sit right beside it.
    ("5+ years of experience in analytics engineering. "
     "Paid month off after 4 years & every 2 years thereafter", 5),
    ("3+ years in an analytics role. Unlimited PTO after 1 year.", 3),
    ("2+ years of experience. 401(k) matching after 1 year of service.", 2),
])
def test_requirements_win_over_nearby_benefits(text, expected):
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
    """TN status is not sponsorship, so this question is answered yes."""
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
    # Major North American hubs rank equal to the preferred three - relocating for
    # the right role is on the table.
    ("Austin, TX, United States", "tier1"),
    ("Seattle, WA", "tier1"),
    ("Vancouver, BC", "tier1"),
    ("Remote US", "tier2"),
    ("North America", "tier2"),
    ("Wichita, KS", "unknown"),
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


@pytest.mark.parametrize("title", [
    "Data Engineer Intern",
    "Data Analyst Co-op",
    "Machine Learning Engineer Trainee",
    "Data Science Summer 2027 Program",
    "2027 Summer Data Analyst",
    "Undergraduate Data Analyst",
    "Data Engineering Student Worker",
    "Analytics Industrial Placement",
    "Data Analyst Placement Year",
    "Data Science Practicum",
])
def test_internship_variants_rejected(title):
    assert not evaluate(make_job(title)).passed, f"{title!r} should be excluded"


def test_structured_employment_type_rejects_internship():
    """An Ashby posting typed "Intern" is an internship whatever the title says."""
    job = make_job("Data Engineer")
    job.employment_type = "Intern"
    assert evaluate(job).reason == "internship_type"


def test_full_time_employment_type_passes():
    job = make_job("Data Engineer")
    job.employment_type = "FullTime"
    assert evaluate(job).passed


@pytest.mark.parametrize("title", [
    # Must NOT be caught: these are real full-time roles.
    "Data Analyst, Student Success",
    "Data Engineer, Summer Products",
    "Analytics Engineer, Placement Services",
])
def test_internship_filter_does_not_overreach(title):
    assert evaluate(make_job(title)).passed, f"{title!r} should be kept"


# ------------------------------------------- Member of Technical Staff regression

DATA_BODY = (
    "Build data pipelines with Spark and Kafka feeding our warehouse. Airflow and "
    "dbt orchestration, streaming ingestion, analytics models. 2+ years of experience."
)
WEB_BODY = "Build React components and REST endpoints for the marketing site."


@pytest.mark.parametrize("title", [
    "Member of Technical Staff",
    "Member of Technical Staff, Data",
    "Member of Technical Staff (AI Inference Engineer)",
])
def test_member_of_technical_staff_is_not_senior(title):
    """Regression: the word "Staff" inside MTS rejected the standard IC title at
    every AI lab, silently removing that whole company tier from consideration."""
    from jobradar.filters import SENIOR_TITLE
    assert not SENIOR_TITLE.search(title), f"{title!r} should not read as staff-level"


@pytest.mark.parametrize("title", [
    "Staff Data Engineer",
    "Staff Analytics Engineer",
    "Senior Member of Technical Staff",
])
def test_genuinely_senior_titles_still_rejected(title):
    from jobradar.filters import SENIOR_TITLE
    assert SENIOR_TITLE.search(title), f"{title!r} should still be rejected"


def test_mts_admitted_only_when_the_body_is_data_centric():
    cfg = {"allow_data_adjacent_swe": True}
    assert evaluate(make_job("Member of Technical Staff", description=DATA_BODY), cfg).passed
    assert not evaluate(make_job("Member of Technical Staff", description=WEB_BODY), cfg).passed


@pytest.mark.parametrize("title,family", [
    ("Research Engineer", "research_engineer"),
    ("Research Scientist", "research_engineer"),
    ("Research Engineer, Frontier Evals", "research_engineer"),
])
def test_research_engineer_classifies(title, family):
    result = classify_role(title)
    assert result is not None and result[0] == family


def test_research_engineer_ranks_below_the_data_families():
    """A real but weaker fit: it should surface without crowding the top."""
    assert classify_role("Research Engineer")[1] < classify_role("Data Engineer")[1]
    assert classify_role("Research Engineer")[1] < classify_role("Data Analyst")[1]
