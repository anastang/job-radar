"""Scoring behaviour, including the normalization that keeps feed sources fair."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobradar.filters import evaluate
from jobradar.models import Job
from jobradar.scoring import Profile, score_job
from jobradar.store import State

REPO = Path(__file__).resolve().parents[1]
PROFILE = REPO / "config" / "profile.yaml"

# Long enough to exercise the real path - actual postings run to thousands of chars.
DESCRIPTION = (
    "You will build dbt models and Airflow DAGs, working with Kafka, Spark and "
    "Databricks on Redshift and PostgreSQL. Python and SQL are required, and you "
    "will own ELT pipelines end to end, partnering with analytics and platform "
    "teams to land trustworthy data. We are looking for 1-3 years of experience "
    "and a strong foundation in data modeling, orchestration and data quality."
)


@pytest.fixture(scope="module")
def profile() -> Profile:
    if not PROFILE.exists():
        pytest.skip("run scripts/build_profile.py first")
    return Profile.load(PROFILE)


def make_job(title="Data Engineer", location="San Francisco, CA", description="",
             hours_old=2.0) -> Job:
    return Job(
        ats="greenhouse", company="acme", external_id="1", title=title,
        url="https://example.com", location_raw=location, description=description,
        posted_at=datetime.now(timezone.utc) - timedelta(hours=hours_old),
    )


def score_for(job: Job, profile: Profile):
    verdict = evaluate(job)
    assert verdict.passed, f"expected pass, got {verdict.reason}"
    return score_job(job, verdict, profile)


def test_profile_loaded_from_resume(profile):
    terms = {t.lower() for t in profile.index}
    for expected in ("dbt", "airflow", "kafka", "databricks", "redshift", "pyspark"):
        assert expected in terms, f"{expected} missing from generated profile"


def test_terms_are_deduped_across_groups(profile):
    """Databricks appears in two taxonomy groups; it must score once."""
    keys = list(profile.index.keys())
    assert len(keys) == len(set(keys))


def test_matched_skills_reported(profile):
    score = score_for(make_job(description=DESCRIPTION), profile)
    matched = {m.lower() for m in score.matched_skills}
    assert {"dbt", "airflow", "kafka"} <= matched
    assert score.skills > 0


def test_target_city_outranks_elsewhere(profile):
    sf = score_for(make_job(location="San Francisco, CA", description=DESCRIPTION), profile)
    wv = score_for(make_job(location="Charleston, WV", description=DESCRIPTION), profile)
    assert sf.total > wv.total


def test_toronto_is_a_target_city(profile):
    toronto = score_for(make_job(location="Toronto, ON", description=DESCRIPTION), profile)
    elsewhere = score_for(make_job(location="Austin, TX", description=DESCRIPTION), profile)
    assert toronto.total > elsewhere.total


def test_fresher_posting_scores_higher(profile):
    new = score_for(make_job(description=DESCRIPTION, hours_old=1), profile)
    old = score_for(make_job(description=DESCRIPTION, hours_old=24 * 10), profile)
    assert new.total > old.total


def test_descriptionless_posting_is_not_crushed(profile):
    """Regression: feed postings have no body, so skill overlap is unmeasurable.

    Charging them the full skills weight anyway ranked a genuine "Data Engineering
    New Grad" below generic roles that merely had text to keyword-match.
    """
    feed = score_for(make_job(title="Data Engineering New Grad", description=""), profile)
    assert feed.total > 60, f"feed posting scored only {feed.total}"


def test_scores_stay_within_range(profile):
    for job in (make_job(description=DESCRIPTION), make_job(), make_job(location="Hybrid")):
        score = score_for(job, profile)
        assert 0 <= score.total <= 100


# -------------------------------------------------------------------- state

def test_state_roundtrip_and_dedupe(tmp_path):
    state = State(tmp_path / "seen.json")
    job = make_job()

    assert state.is_new(job)
    state.mark_seen(job, score=80.0, notified=True)
    state.save()

    reloaded = State(tmp_path / "seen.json").load()
    assert not reloaded.is_new(job), "a seen job must never alert twice"
    assert reloaded.was_notified(job)
    assert len(reloaded) == 1


def test_state_prunes_old_entries(tmp_path):
    state = State(tmp_path / "seen.json")
    state.mark_seen(make_job())
    old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    state.seen["stale-plain"] = old[:10]
    state.matches["stale-match"] = {"first_seen": old, "title": "Ancient"}

    assert state.prune(days=90) == 2
    assert "stale-plain" not in state.seen
    assert "stale-match" not in state.matches
    assert len(state) == 1


def test_unmatched_postings_stored_compactly(tmp_path):
    """The bulk of state is committed every 5 minutes; it must stay small."""
    state = State(tmp_path / "seen.json")
    state.mark_seen(make_job())
    assert len(state.seen) == 1 and not state.matches
    assert isinstance(next(iter(state.seen.values())), str)


def test_scored_posting_promoted_to_full_record(tmp_path):
    state = State(tmp_path / "seen.json")
    job = make_job()
    state.mark_seen(job)
    state.mark_seen(job, score=81.0, notified=True)

    assert job.key not in state.seen, "must not be recorded in both places"
    assert state.matches[job.key]["score"] == 81.0
    assert state.was_notified(job)


def test_v1_state_migrates(tmp_path):
    """Older state files kept every posting as a full record under "jobs"."""
    path = tmp_path / "seen.json"
    path.write_text(json.dumps({
        "version": 1,
        "jobs": {
            "plain": {"first_seen": "2026-08-01T00:00:00+00:00", "title": "A"},
            "scored": {"first_seen": "2026-08-01T00:00:00+00:00", "score": 70.0,
                       "notified": True, "title": "B"},
        },
    }), encoding="utf-8")

    state = State(path).load()
    assert state.seen["plain"] == "2026-08-01"
    assert state.matches["scored"]["score"] == 70.0
    assert len(state) == 2


def test_corrupt_state_does_not_crash(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert len(State(path).load()) == 0


def test_untrusted_dates_score_neutrally(profile):
    """A feed date can be weeks wrong, so it must not swing freshness either way."""
    fresh = Job(ats="simplify", company="a", external_id="1", title="Data Engineer",
                url="", location_raw="San Francisco, CA",
                posted_at=datetime.now(timezone.utc) - timedelta(hours=1))
    old = Job(ats="simplify", company="a", external_id="2", title="Data Engineer",
              url="", location_raw="San Francisco, CA",
              posted_at=datetime.now(timezone.utc) - timedelta(days=40))
    a, b = score_for(fresh, profile), score_for(old, profile)
    assert a.total == b.total, "feed dates must not affect score"
    assert 0 < a.freshness < 100, "should be neutral, not zero or full marks"


def test_trusted_dates_still_drive_freshness(profile):
    new = Job(ats="greenhouse", company="a", external_id="1", title="Data Engineer",
              url="", location_raw="San Francisco, CA", description=DESCRIPTION,
              posted_at=datetime.now(timezone.utc) - timedelta(hours=1))
    old = Job(ats="greenhouse", company="a", external_id="2", title="Data Engineer",
              url="", location_raw="San Francisco, CA", description=DESCRIPTION,
              posted_at=datetime.now(timezone.utc) - timedelta(days=10))
    assert score_for(new, profile).total > score_for(old, profile).total
