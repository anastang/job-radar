"""Company-discovery helpers.

These matter more than their size suggests. An audit of eight companies found by hand
on LinkedIn showed four were reachable on boards this tool already polls, and were
missed purely because the company list did not name them. Slug guessing is what turns
a company name into something pollable, so its edge cases are worth pinning down.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(script: str):
    """scripts/ is not a package, so load the module from its path."""
    path = REPO / "scripts" / script
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def add_company():
    return _load("add_company.py")


@pytest.fixture(scope="module")
def discover_vc():
    return _load("discover_vc.py")


@pytest.mark.parametrize("name,expected", [
    # Real resolutions: "Distyl AI" is board "distyl", "Charta Health" is
    # "chartahealth". Both forms have to be generated or one of them is missed.
    ("Distyl AI", "distyl"),
    ("Charta Health", "chartahealth"),
    ("Coalition", "coalition"),
    ("Lovable", "lovable"),
    ("Modern Treasury", "moderntreasury"),
    ("Sandbox VR", "sandboxvr"),
])
def test_candidate_slugs_include_the_real_board_name(add_company, name, expected):
    assert expected in add_company.candidate_slugs(name)


def test_candidate_slugs_strips_common_suffix_noise(add_company):
    """"AI", "Labs", "Inc" and similar are routinely dropped from board slugs."""
    assert "distyl" in add_company.candidate_slugs("Distyl AI")
    assert "charta" in add_company.candidate_slugs("Charta Health")


def test_candidate_slugs_are_deduped_and_bounded(add_company):
    slugs = add_company.candidate_slugs("Ramp")
    assert len(slugs) == len(set(slugs))
    assert all(len(s) >= 3 for s in slugs)
    # A single short word should not explode into noise.
    assert len(slugs) <= 4


def test_candidate_slugs_handles_junk(add_company):
    assert add_company.candidate_slugs("") == []
    assert add_company.candidate_slugs("!!") == []


def test_vc_extraction_skips_exit_annotations(discover_vc):
    """Portfolio pages label exits inline; those are not companies to poll."""
    page = ('"title":"Braintrust" "title":"Acquired By: Appriss Health" '
            '"title":"Resend" "title":"IPO: Something"')
    names = discover_vc.extract_names(page, r'"title":"([^"]{2,40})"')
    assert "Braintrust" in names and "Resend" in names
    assert not any(n.lower().startswith(("acquired by", "ipo")) for n in names)


def test_vc_only_verified_sources_are_configured(discover_vc):
    """Adding an unverified portfolio would contribute zero and look like it worked."""
    assert set(discover_vc.SOURCES) == {"a16z"}
