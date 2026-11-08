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
import yaml

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
    """Each source was checked against the live page before being added.

    Accel, Techstars and Creative Destruction Lab render their listings client-side
    and yield zero names from the markup; Sequoia yields 21 household brands already
    covered elsewhere. Adding any of them would contribute nothing while appearing
    to work, so they stay out.
    """
    assert set(discover_vc.SOURCES) == {
        "a16z", "index", "foundersfund", "generalcatalyst"
    }
    for url, pattern, firm in discover_vc.SOURCES.values():
        assert url.startswith("https://")
        assert pattern and firm


def test_vc_strips_the_firm_name_from_labels(discover_vc):
    """Founders Fund labels each entry "Affirm - Founders Fund"."""
    page = '"name":"Affirm - Founders Fund" "name":"Airbnb - Founders Fund"'
    names = discover_vc.extract_names(page, r'"name"\s*:\s*"([^"]{2,40})"', "Founders Fund")
    assert names == ["Affirm", "Airbnb"]


def test_vc_drops_navigation_chrome(discover_vc):
    """Filter and nav labels sit in the same markup as the company names."""
    page = ('<h2>All</h2><h2>Portfolio</h2><h2>Series A</h2>'
            '<h2>Aaru</h2><h2>Accordance</h2>')
    names = discover_vc.extract_names(page, r'<h[23][^>]*>([^<]{2,40})</h[23]>', "")
    assert names == ["Aaru", "Accordance"]


# ------------------------------------------------ aggregator sitemap discovery

@pytest.fixture(scope="module")
def boards():
    return _load("discover_from_boards.py")


def test_startupjobs_split_uses_the_published_company_list(boards):
    """Nothing in a startup.jobs URL marks where the title ends.

    The slug is one hyphen-run holding both halves, and company slugs contain
    hyphens too. Guessing the last token produced companies called "1", "10" and
    "091614", so the split is checked against the slugs startup.jobs publishes.
    """
    known = {"palantir", "general-catalyst"}
    assert boards.split_startupjobs(
        "forward-deployed-software-engineer-palantir", known
    ) == ("forward deployed software engineer", "palantir")


def test_startupjobs_split_prefers_the_longest_company_match(boards):
    """"general-catalyst" has to win over "catalyst"."""
    known = {"catalyst", "general-catalyst"}
    title, company = boards.split_startupjobs("data-engineer-general-catalyst", known)
    assert company == "general-catalyst"
    assert title == "data engineer"


def test_startupjobs_split_declines_unknown_companies(boards):
    """Returning a guess here is what produced the numeric junk companies."""
    assert boards.split_startupjobs("data-engineer-whoever", {"palantir"}) is None


def test_bulk_import_prefixes_are_stripped_from_company_slugs(boards):
    """startup.jobs carries legal-entity slugs from an import."""
    assert boards.clean_company("0020-paypal-inc") == "paypal inc"
    assert boards.clean_company("100-salesforce-inc") == "salesforce inc"
    assert boards.clean_company("1000-kla-corporation-2") == "kla corporation"


def test_a_name_that_is_legitimately_numeric_survives(boards):
    """"100ms" is a real company, so the prefix strip must not swallow it."""
    assert boards.clean_company("100ms") == "100ms"
    assert boards.clean_company("1kosmos") == "1kosmos"


def test_relevant_title_matches_the_roles_being_searched_for(boards):
    for title in ("data engineer", "analytics engineer", "ai engineer",
                  "forward deployed software engineer", "software engineer ii",
                  "data analyst"):
        assert boards.relevant_title(title), title


def test_relevant_title_skips_what_the_gate_would_reject(boards):
    for title in ("senior data engineer", "data engineer intern",
                  "account executive platforms grower", "staff software engineer",
                  "data entry associate"):
        assert not boards.relevant_title(title), title


def test_known_slugs_compares_only_the_leading_path_segment(boards, tmp_path):
    """Workday and Oracle slugs are compound; only the host part is comparable."""
    (tmp_path / "companies_x.yaml").write_text(
        "workday:\n  tier1: []\n  tier2:\n    - acme.wd5/acme/careers\n"
        "greenhouse:\n  tier1:\n    - stripe\n  tier2: []\n",
        encoding="utf-8",
    )
    known = boards.known_slugs(tmp_path)
    assert "acmewd5" in known and "stripe" in known


def test_hiringtempo_sitemap_parsing(boards):
    body = (
        "<url><loc>https://hiringtempo.com/company/cerebras/jobs/"
        "data-engineer-060e3115</loc></url>"
        "<url><loc>https://hiringtempo.com/about</loc></url>"
    )
    assert boards.parse_sitemap("hiringtempo", body) == [("cerebras", "data engineer")]


def test_himalayas_sitemap_parsing(boards):
    body = ("<loc>https://himalayas.app/companies/onepilot/jobs/data-engineer</loc>"
            "<loc>https://himalayas.app/companies/zapier</loc>")
    assert boards.parse_sitemap("himalayas", body) == [("onepilot", "data engineer")]


def test_himalayas_strips_a_numeric_id_glued_to_the_title(boards):
    """Some himalayas job slugs end in the posting id, some do not."""
    body = ("<loc>https://himalayas.app/companies/search-atlas/jobs/"
            "product-engineer-pm-5642318840</loc>")
    assert boards.parse_sitemap("himalayas", body) == [
        ("search-atlas", "product engineer pm")]


def test_every_configured_source_has_a_parser(boards):
    """A source in SITEMAPS with no branch in parse_sitemap silently yields zero."""
    for source in boards.SITEMAPS:
        assert source in ("hiringtempo", "himalayas", "startupjobs"), \
            f"{source} has no parse_sitemap branch"


def test_generated_board_file_accumulates_across_runs(boards, tmp_path, monkeypatch):
    """The pool takes many batches, so a run must add to the file, not replace it.

    Measured regression: as a plain write, the second run took the file from 188
    boards down to 126. Worse than the loss, it un-knew those companies, so the next
    run would spend its whole budget re-resolving them and the list would thrash
    instead of growing.
    """
    import asyncio

    out = tmp_path / "companies_boards.yaml"
    out.write_text(
        "ashby:\n  tier1: []\n  tier2:\n    - cerebras\n    - chainguard\n"
        "greenhouse:\n  tier1: []\n  tier2:\n    - coreweave\n",
        encoding="utf-8",
    )

    # Resolve one new company and nothing else; the three above must survive.
    async def fake_resolve(fetcher, name):
        return ("lever", "newco", 3) if name == "newco" else None

    monkeypatch.setattr(boards.add_company, "resolve", fake_resolve)
    monkeypatch.setattr(boards, "SITEMAPS", {"hiringtempo": ["https://example.test/s.xml"]})
    monkeypatch.setattr(boards, "DEFAULT_SOURCES", ["hiringtempo"])

    async def fake_sitemap(fetcher, url):
        return ("<loc>https://hiringtempo.com/company/newco/jobs/"
                "data-engineer-aabbccdd</loc>")

    monkeypatch.setattr(boards, "fetch_sitemap", fake_sitemap)
    monkeypatch.setattr("sys.argv", ["discover_from_boards.py", "--out", str(out)])
    asyncio.run(boards.main())

    merged = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert set(merged["ashby"]["tier2"]) == {"cerebras", "chainguard"}
    assert set(merged["greenhouse"]["tier2"]) == {"coreweave"}
    assert set(merged["lever"]["tier2"]) == {"newco"}
