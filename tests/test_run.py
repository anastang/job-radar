"""Orchestrator wiring: company-file merging and tier scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobradar.run import due, load_companies, select_targets
from jobradar.store import State


def write(path, text):
    path.write_text(text, encoding="utf-8")


def test_merges_every_companies_file(tmp_path):
    """validate_companies.py and discover_yc.py each own a file; both must be polled."""
    write(tmp_path / "companies.yaml",
          "greenhouse:\n  tier1:\n    - stripe\n  tier2:\n    - lyft\n")
    write(tmp_path / "companies_yc.yaml",
          "ashby:\n  tier1: []\n  tier2:\n    - modal\n")
    write(tmp_path / "companies_feed.yaml",
          "workday:\n  tier1: []\n  tier2:\n    - nvidia.wd5/nvidia/job\n")

    merged = load_companies(tmp_path / "companies.yaml")
    assert merged["greenhouse"]["tier1"] == ["stripe"]
    assert merged["ashby"]["tier2"] == ["modal"]
    assert merged["workday"]["tier2"] == ["nvidia.wd5/nvidia/job"]


def test_merge_dedupes_and_prefers_tier1(tmp_path):
    """A slug promoted to tier1 must not also be polled on the tier2 cadence."""
    write(tmp_path / "companies.yaml",
          "ashby:\n  tier1:\n    - ramp\n  tier2:\n    - modal\n")
    write(tmp_path / "companies_yc.yaml",
          "ashby:\n  tier1: []\n  tier2:\n    - ramp\n    - modal\n")

    merged = load_companies(tmp_path / "companies.yaml")
    assert merged["ashby"]["tier1"] == ["ramp"]
    assert merged["ashby"]["tier2"] == ["modal"]


def test_missing_files_are_not_fatal(tmp_path):
    assert load_companies(tmp_path / "nope.yaml") == {}


def test_select_targets_respects_tier_and_known_adapters(tmp_path):
    write(tmp_path / "companies.yaml",
          "greenhouse:\n  tier1:\n    - stripe\n  tier2:\n    - lyft\n"
          "bogus_ats:\n  tier1:\n    - nope\n")
    merged = load_companies(tmp_path / "companies.yaml")

    tier1_only = select_targets(merged, include_tier2=False)
    assert tier1_only == [("greenhouse", "stripe")]

    both = select_targets(merged, include_tier2=True)
    assert set(both) == {("greenhouse", "stripe"), ("greenhouse", "lyft")}
    assert not any(ats == "bogus_ats" for ats, _ in both), "unknown ATS must be ignored"


def test_due_schedules_tier2_on_cadence(tmp_path):
    state = State(tmp_path / "seen.json")
    assert due(state, "last_tier2", 30), "no record yet means run it"

    state.meta["last_tier2"] = datetime.now(timezone.utc).isoformat()
    assert not due(state, "last_tier2", 30)
    assert due(state, "last_tier2", 30, force=True)

    state.meta["last_tier2"] = (
        datetime.now(timezone.utc) - timedelta(minutes=45)
    ).isoformat()
    assert due(state, "last_tier2", 30)


def test_due_tolerates_corrupt_timestamp(tmp_path):
    state = State(tmp_path / "seen.json")
    state.meta["last_tier2"] = "not-a-date"
    assert due(state, "last_tier2", 30), "unparseable timestamp should not wedge polling"
