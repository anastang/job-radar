"""Rippling adapter.

Rippling is the only provider that splits the data across two endpoints, so these
tests pin the parts that make the two-phase fetch safe: the title pre-filter can
only discard what the real gate would reject anyway, and the detail parsing handles
the three fields Rippling shapes differently from everyone else.
"""

from __future__ import annotations

from jobradar.sources import rippling

LISTING = {
    "uuid": "abc-123",
    "name": " Software Engineer II, Backend ",
    "department": {"id": "Engineering", "label": "Engineering"},
    "url": "https://ats.rippling.com/acme/jobs/abc-123",
    "workLocation": {"label": "CA", "id": "CA"},
}

DETAIL = {
    "uuid": "abc-123",
    "name": "Software Engineer II, Backend",
    "createdOn": "2026-10-06T16:36:07.947000-07:00",
    # Sectioned object, not a string, and each section holds HTML.
    "description": {
        "company": "<p>Acme builds <b>things</b>.</p>",
        "role": "<ul><li>Own our FastAPI services</li><li>Write pytest suites</li></ul>",
    },
    "employmentType": {"label": "SALARIED_FT", "id": "Salaried, full-time"},
    "workLocations": ["San Francisco, CA", "Seattle, WA"],
    "payRangeDetails": [
        {"location": "SF", "currency": "USD", "frequency": "YEAR",
         "rangeStart": 135000.0, "rangeEnd": 225000.0, "isRemote": False},
        {"location": "NY", "currency": "USD", "frequency": "YEAR",
         "rangeStart": 150000.0, "rangeEnd": 210000.0, "isRemote": False},
    ],
    "unlistedFromSearch": False,
    "url": "https://ats.rippling.com/acme/jobs/abc-123",
}


def test_build_merges_listing_and_detail():
    job = rippling.build("acme", LISTING, DETAIL)
    assert job is not None
    assert job.ats == "rippling"
    assert job.external_id == "abc-123"
    assert job.title == "Software Engineer II, Backend"
    assert job.posted_at is not None and job.posted_at.year == 2026
    assert job.date_trusted, "rippling reports the employer's own createdOn"


def test_sectioned_html_description_is_flattened():
    """The field is an object keyed by section, so a plain decode yields nothing."""
    job = rippling.build("acme", LISTING, DETAIL)
    assert "Acme builds things" in job.description
    assert "FastAPI" in job.description and "pytest" in job.description
    assert "<" not in job.description, "HTML should be stripped"


def test_detail_locations_win_over_the_listing_label():
    """The listing carries one bare state code; the detail carries the real list."""
    job = rippling.build("acme", LISTING, DETAIL)
    assert job.location_raw == "San Francisco, CA; Seattle, WA"


def test_employment_type_is_read_from_the_id_field():
    """Rippling inverts the usual shape: the readable value is in `id`."""
    job = rippling.build("acme", LISTING, DETAIL)
    assert job.employment_type == "Salaried, full-time"


def test_salary_spans_the_widest_annual_band():
    job = rippling.build("acme", LISTING, DETAIL)
    assert (job.salary_min, job.salary_max) == (135000.0, 225000.0)
    assert job.salary_currency == "USD"


def test_non_annual_pay_is_dropped_rather_than_published():
    """A monthly figure shown as a salary would read as a tenth of the real offer."""
    detail = dict(DETAIL, payRangeDetails=[
        {"currency": "USD", "frequency": "MONTH", "rangeStart": 10500.0,
         "rangeEnd": 10500.0},
    ])
    job = rippling.build("acme", LISTING, detail)
    assert job.salary_min is None and job.salary_max is None


def test_postings_hidden_from_search_are_skipped():
    """Rippling delists rather than deleting, so these are not live roles."""
    assert rippling.build("acme", LISTING, dict(DETAIL, unlistedFromSearch=True)) is None


def test_garbage_detail_does_not_raise():
    for bad in ({}, {"uuid": "x"}, {"uuid": "x", "name": ""}):
        rippling.build("acme", {"uuid": "x"}, bad)


# ------------------------------------------------------- the detail pre-filter

def test_pre_filter_keeps_titles_the_gate_could_admit():
    for title in ("Software Engineer I - Data Infrastructure", "Data Engineer",
                  "Analytics Engineer", "AI Engineer",
                  "Forward Deployed Software Engineer", "Software Engineer II"):
        assert rippling.title_is_worth_detail(title), title


def test_pre_filter_drops_what_the_gate_would_reject_anyway():
    """It must only ever discard postings `evaluate` would also reject.

    Otherwise the saved request hides a role that would have alerted. Each of these
    is rejected by a title-level gate in filters.py: seniority, internship, or the
    negative-title list.
    """
    for title in ("Senior Software Engineer", "Staff Data Engineer",
                  "Engineering Manager", "Software Engineer Intern",
                  "Data Engineer Co-op", "Account Executive",
                  "Recruiter", "Data Entry Clerk"):
        assert not rippling.title_is_worth_detail(title), title


def test_pre_filter_drops_unrelated_departments():
    """665 postings reduce to 19 because most of a board is not engineering."""
    for title in ("Payroll Specialist", "Customer Support Advocate",
                  "Product Designer", "Benefits Consultant"):
        assert not rippling.title_is_worth_detail(title), title


def test_pre_filter_handles_junk():
    assert not rippling.title_is_worth_detail("")
    assert not rippling.title_is_worth_detail("   ")
