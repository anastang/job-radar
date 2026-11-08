"""Outreach links on the alert.

The decision worth protecting here is a negative one: addresses are not scraped out
of postings. Measured across 4,007 described postings, 97.4% of the emails that
appear in a job body are accommodation or accessibility addresses, so an "extract
the contact" feature would almost always surface an ADA compliance queue and label
it a hiring contact. These tests pin the alternative in place.
"""

from __future__ import annotations

import inspect
from urllib.parse import unquote_plus

from jobradar.notify import outreach


def test_field_offers_both_a_recruiter_and_a_functional_lead():
    field = outreach.outreach_field("Cerebras", "data")
    assert field is not None
    value = field["value"]
    assert "Recruiters" in value
    assert "Head Of Data" in value
    assert value.count("https://www.linkedin.com") == 2


def test_the_lead_searched_for_follows_the_resume_track():
    leads = {
        track: unquote_plus(outreach.people_search("Acme", outreach.LEAD_TITLE[track]))
        for track in ("data", "ai", "fde", "swe")
    }
    assert "head of data" in leads["data"]
    assert "head of AI" in leads["ai"]
    assert "head of solutions" in leads["fde"]
    assert "engineering manager" in leads["swe"]


def test_an_unknown_track_still_gets_a_usable_search():
    field = outreach.outreach_field("Acme", "")
    assert field is not None and "Engineering Manager" in field["value"]


def test_company_names_are_url_encoded():
    url = outreach.people_search("Charta Health", "recruiter")
    assert " " not in url
    assert "Charta+Health" in url


def test_no_company_means_no_field():
    """A handful of feed rows carry no company name."""
    assert outreach.outreach_field("", "data") is None
    assert outreach.outreach_field("   ", "data") is None


def test_the_field_fits_discord_limits():
    field = outreach.outreach_field("A" * 300, "data")
    assert len(field["value"]) <= 1024


def test_nothing_here_reads_a_posting_body():
    """Regression guard on the measured decision.

    If an email-extraction regex ever lands in this module, it will be scraping
    accommodation addresses 97 times out of 100. The alternative is a search the
    user runs as themselves, which touches no personal data at all.
    """
    source = inspect.getsource(outreach)
    assert "@" not in source.replace("@company.com", ""), \
        "outreach must not parse or build email addresses"
    for banned in ("description", "haystack", "hunter", "apollo", "rocketreach"):
        assert banned not in source.lower(), f"outreach must not touch {banned}"
