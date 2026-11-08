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


def test_all_four_resume_tracks_are_loaded(profile):
    """One track per tailored resume, each naming the file to attach."""
    assert {t.name for t in profile.tracks} == {"data", "ai", "fde", "swe"}
    for track in profile.tracks:
        assert track.resume, f"{track.name} names no resume file"
        assert track.index, f"{track.name} has no terms"


def test_each_track_carries_its_own_signature_vocabulary(profile):
    """The tracks have to be distinguishable, or picking between them is noise."""
    def terms(name):
        return {t.lower() for t in profile.get(name).index}

    assert {"dbt", "star schema", "power bi"} <= terms("data")
    assert {"langchain", "pgvector", "llm judge"} <= terms("ai")
    assert {"forward deployed", "stakeholder", "workshop"} <= terms("fde")
    assert {"react", "pytest", "microservices"} <= terms("swe")
    # And each must hold something the others do not.
    for name in ("data", "ai", "fde", "swe"):
        others = set().union(*(terms(o) for o in ("data", "ai", "fde", "swe")
                               if o != name))
        assert terms(name) - others, f"{name} has no vocabulary of its own"


def test_terms_are_deduped_within_a_track(profile):
    """A term in two groups of one track must score once, at its highest weight."""
    for track in profile.tracks:
        keys = list(track.index.keys())
        assert len(keys) == len(set(keys)), f"{track.name} double-counts a term"


def test_shared_infrastructure_is_not_core_to_any_track(profile):
    """Python, Docker and AWS are in every posting, so they cannot signal a track.

    If they counted as core the four tracks would look alike, and the generic
    engineering gate would admit any backend posting that mentions them.
    """
    for track in profile.tracks:
        core = track.core_terms()
        for universal in ("Python", "Docker", "AWS"):
            assert not any(p.search(universal) for p in core),                 f"{track.name} treats {universal} as core"


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
    small_town = score_for(make_job(location="Wichita, KS", description=DESCRIPTION), profile)
    assert toronto.total > small_town.total


def test_major_hubs_rank_equally(profile):
    """Relocating for the right role is on the table, so hubs are not penalised."""
    scores = {
        city: score_for(make_job(location=city, description=DESCRIPTION), profile).total
        for city in ("Toronto, ON", "San Francisco, CA", "New York, NY",
                     "Seattle, WA", "Austin, TX")
    }
    assert len(set(scores.values())) == 1, scores


def test_consultancies_rank_below_product_companies(profile):
    consult = Job(ats="greenhouse", company="Kyndryl", external_id="1",
                  title="Data Engineer", url="", location_raw="Toronto, ON",
                  description=DESCRIPTION,
                  posted_at=datetime.now(timezone.utc) - timedelta(hours=2))
    normal = Job(ats="greenhouse", company="Ramp", external_id="2",
                 title="Data Engineer", url="", location_raw="Toronto, ON",
                 description=DESCRIPTION,
                 posted_at=datetime.now(timezone.utc) - timedelta(hours=2))
    assert score_for(consult, profile).total < score_for(normal, profile).total


def test_startups_get_a_boost(profile):
    startup = Job(ats="ashby", company="minerva", external_id="1",
                  title="Data Engineer", url="", location_raw="New York, NY",
                  description=DESCRIPTION,
                  posted_at=datetime.now(timezone.utc) - timedelta(hours=2))
    other = Job(ats="ashby", company="bigcorp", external_id="2",
                title="Data Engineer", url="", location_raw="New York, NY",
                description=DESCRIPTION,
                posted_at=datetime.now(timezone.utc) - timedelta(hours=2))
    v = evaluate(startup)
    boosted = score_job(startup, v, profile, startup_slugs=frozenset({"minerva"}))
    plain = score_job(other, evaluate(other), profile, startup_slugs=frozenset({"minerva"}))
    assert boosted.total > plain.total
    assert boosted.company_kind == "startup"


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


# ------------------------------------------------- picking the right resume

GATE = {"max_years_experience": 3, "allow_data_adjacent_swe": True,
        "allow_unknown_location": True}

TRACK_CASES = [
    ("data", "Analytics Engineer",
     "Build and own dbt models on Snowflake with Airflow orchestration. You will "
     "design star schema dimensional models, add data quality tests and freshness "
     "SLAs, and serve a semantic layer to Looker dashboards. Strong SQL. 1-2 years."),
    ("ai", "AI Engineer",
     "Ship LLM features end to end. You will build RAG pipelines with LangChain over "
     "a pgvector store, tune chunking and reranking with a cross-encoder, and gate "
     "every prompt change with an eval harness and an LLM judge. Tracing in Langfuse. "
     "Serve inference through FastAPI at low p95 latency. 1-3 years."),
    ("fde", "Forward Deployed Software Engineer",
     "You will sit with customers on site, scope their workflows with stakeholders, "
     "and build bespoke integrations against their systems. Expect to run a pilot, "
     "demo to leadership, win sign-off, and own the rollout end to end, including "
     "workshops to train end users. REST API and connector work in Python. 2 years."),
    ("swe", "Software Engineer II, Full Stack",
     "Build product surfaces in React and TypeScript on a FastAPI backend. You will "
     "own REST API design across our microservices, write pytest unit and integration "
     "tests, and keep the GitHub Actions CI pipeline green. PostgreSQL schema design "
     "and query optimization. Redis caching. 1-3 years of experience."),
]


@pytest.mark.parametrize("expected,title,description", TRACK_CASES)
def test_posting_is_routed_to_the_matching_resume(profile, expected, title, description):
    """The winning track is the answer to "which resume do I send", so it has to be
    right. Each of these is written the way real postings in that discipline are."""
    job = make_job(title=title, description=description)
    verdict = evaluate(job, GATE, profile.core_terms(),
                       profile.get("swe").core_terms())
    assert verdict.passed, verdict.reason
    score = score_job(job, verdict, profile)
    assert score.track == expected, f"sent the {score.track} resume: {score.track_summary()}"
    assert score.resume, "no resume filename to attach"


def test_the_winning_track_leads_the_others_clearly(profile):
    """A near-tie would mean the track vocabularies overlap too much to be useful."""
    for expected, title, description in TRACK_CASES:
        job = make_job(title=title, description=description)
        verdict = evaluate(job, GATE, profile.core_terms(),
                           profile.get("swe").core_terms())
        score = score_job(job, verdict, profile)
        ranked = sorted(score.track_scores.values(), reverse=True)
        assert ranked[0] - ranked[1] > 0.2,             f"{title}: tracks too close -> {score.track_summary()}"


def _track_for(profile, title, body):
    job = make_job(title=title, description=body)
    verdict = evaluate(job, GATE, profile.core_terms(),
                       profile.get("swe").core_terms())
    assert verdict.passed, verdict.reason
    return score_job(job, verdict, profile).track


# Data 0.77 against AI 0.72: inside TRACK_TITLE_MARGIN, so the title decides.
CLOSE_BODY = (
    "Own dbt models and Airflow DAGs on Spark, designing star schema marts and "
    "Power BI reporting. You will also build RAG features with LangChain and "
    "embeddings. Python and SQL. 1-3 years."
)


def test_a_close_race_is_broken_by_the_job_title(profile):
    """Same body, two titles, two different resumes.

    The title is what a reviewer screens the resume against, so when two tracks are
    within a hair of each other it decides. Without this rule the resume sent would
    flip on a single incidental keyword.
    """
    assert _track_for(profile, "Data Engineer", CLOSE_BODY) == "data"
    assert _track_for(profile, "AI Engineer", CLOSE_BODY) == "ai"


def test_the_title_cannot_override_a_decisive_skill_gap(profile):
    """A mislabelled posting is answered with the resume that actually fits.

    This body is unambiguously AI work. Titling it "Data Engineer" must not send the
    Data resume, because the margin only breaks ties - it does not let the title
    outvote the evidence.
    """
    body = ("Build RAG pipelines with LangChain over pgvector, tune chunking and "
            "reranking with a cross-encoder, and gate prompt changes with an eval "
            "harness and an LLM judge. Tracing in Langfuse. Serve inference through "
            "FastAPI. 1-3 years.")
    assert _track_for(profile, "Data Engineer", body) == "ai"


def test_shared_core_pool_can_exclude_the_swe_track(profile):
    """The two generic-engineering routes have to ask different questions.

    Measured regression: with the SWE vocabulary inside the shared pool, "React"
    plus "pytest" counted as two adjacency hits, so 354 postings were admitted by
    the loose route and the stricter SWE-resume route never fired once.
    """
    shared = profile.core_terms(exclude=("swe",))
    swe_only = profile.get("swe").core_terms()
    assert len(shared) < len(profile.core_terms())
    for swe_term in ("React", "pytest", "microservices"):
        assert not any(p.search(swe_term) for p in shared), \
            f"{swe_term} must not count toward the adjacency route"
        assert any(p.search(swe_term) for p in swe_only), f"{swe_term} missing from SWE"


# Phrases that appear in job postings of every kind. A core term matching any of
# these measures whether a posting is written in corporate English, not whether it
# fits a track, and it corrupts the resume recommendation.
#
# Measured regression: the FDE track once scored bare "requirements",
# "cross-functional", "leadership", "executive" and "training" at weight 2.75. On
# 2,378 postings carrying no deployment-ish title those fired on 52%, 39%, 34%, 30%
# and 29% respectively, giving FDE a median 0.412 overlap on roles with nothing
# forward-deployed about them and handing it 136 wrong resume recommendations.
GENERIC_POSTING_PHRASES = (
    "gather requirements from partner teams",
    "work cross-functional with other groups",
    "report to engineering leadership",
    "present to executive sponsors",
    "paid training and development budget",
    "discovery calls with prospects",
    "collaborate with partner teams on delivery",
)


@pytest.mark.parametrize("phrase", GENERIC_POSTING_PHRASES)
def test_no_track_treats_generic_posting_prose_as_a_core_signal(profile, phrase):
    for track in profile.tracks:
        hit = [p.pattern for p in track.core_terms() if p.search(phrase)]
        assert not hit, f"{track.name} matches generic prose {phrase!r} via {hit}"


def test_fde_still_recognises_real_deployment_language(profile):
    """The prune has to leave the signal intact, not just remove the noise."""
    real = ("Sit on site with the customer, run a pilot, win sign-off, run user "
            "training workshops and own the rollout. Forward deployed work.")
    frac, matched = profile.get("fde").fraction(real)
    assert frac > 0.5, f"FDE no longer recognises its own language: {matched}"


def test_the_fde_track_does_not_read_model_training_as_stakeholder_training(profile):
    """The exact original failure, pinned.

    Bare "training" in the FDE stakeholder group matched "model training", so every
    ML posting looked like it involved training customer staff. "model training"
    remains a real signal for the AI track, which is why this is asserted per track
    rather than globally.
    """
    phrase = "distributed model training on GPU clusters"
    assert not any(p.search(phrase) for p in profile.get("fde").core_terms())
    assert any(p.search(phrase) for p in profile.get("ai").core_terms())
