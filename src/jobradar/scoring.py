"""Resume-match scoring across four tailored resumes.

Deliberately deterministic and dependency-free - no model calls on the hot path.
Latency is the entire point of this tool, and a keyword/weight model is both fast
enough to run on every poll and explainable, which matters because the Discord alert
shows *why* a role matched.

Four resumes exist (Data, AI, FDE, SWE), so a posting is scored against all four and
keeps its best result. That is not just a scoring refinement: the winning track is
the answer to "which resume do I send", which is the first decision to make once a
role is worth applying to. A single blended profile cannot answer it, and scores
worse besides - averaging the four would judge a React-and-pytest posting on dbt
terms it never mentions and a dbt posting on React terms it never mentions, rating
both mediocre rather than recognising each as a strong fit for one track.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .filters import FAMILY_TRACK, Verdict
from .models import Job

# Point budget, summing to 100 so thresholds read like percentages.
#
# Skill overlap is the dominant term by design, and it grew when the four tracks
# landed. The job title only establishes that a role is relevant at all; what makes a
# strong *candidate* is how much of the posting's stack one of the resumes has
# actually shipped. Family weight shrank to match: with Data, AI, FDE and SWE all
# first-class targets their family weights sit at or near 1.0, so family no longer
# discriminates much between wanted roles and its old weight was mostly dead points.
W_SKILLS = 42.0
W_FAMILY = 16.0
# Location carries real weight because SF / NYC / Toronto is a stated preference.
# Without it a junior analyst role in a non-target city outranked a strong Bay Area
# match.
W_LOCATION = 20.0
# Experience fit carries real weight. At 6 points a role wanting three years scored
# about the same as a new-grad one, so 4-8 year postings were reaching the top of the
# list purely on stack overlap.
W_EARLY = 12.0
# Company character: IT consultancies and staffing firms post enormous volumes of
# data-titled roles that are often placements rather than product work, while small
# startups are where being early pays most.
W_COMPANY = 6.0
W_FRESH = 4.0

# tier2 is remote and smaller cities only - the major hubs are tier1 - and sits high
# because a remote or second-city role is still well worth seeing.
LOCATION_POINTS = {"tier1": 1.0, "tier2": 0.65, "unknown": 0.35, "reject": 0.0}

COMPANY_POINTS = {"startup": 1.0, "neutral": 0.6, "consultancy": 0.0}

# Groups that describe shared infrastructure rather than the thing a track is for.
# Python, Docker, AWS and PostgreSQL appear in nearly every engineering posting, so
# counting them as evidence of a track match would make all four tracks look alike.
# Everything not listed here is "core" and is what decides whether a generic
# engineering title is worth surfacing at all.
NON_CORE_GROUPS = frozenset({
    "foundational", "platform", "adjacent", "infra", "storage", "data_plumbing",
})

# How close a track has to be to the leader before the job title breaks the tie.
# A posting titled "AI Engineer" whose body happens to name one more data tool than
# AI tool should still be answered with the AI resume, because the title is what the
# reviewer screens on. Only applied when the gap is small; a title cannot rescue a
# track the posting barely matches.
TRACK_TITLE_MARGIN = 0.10

# Matched as substrings against the company name, case-insensitively.
CONSULTANCIES = (
    "kyndryl", "guidehouse", "deloitte", "accenture", "infosys", "cognizant",
    "wipro", "capgemini", "genpact", "ltimindtree", "tech mahindra", "hcl",
    "dxc", "atos", "ntt data", "unisys", "epam", "globant", "slalom",
    "west monroe", "huron", "protiviti", "wavestone", "exl", "mindtree",
    "robert half", "insight global", "teksystems", "apex systems", "randstad",
    "adecco", "kforce", "collabera", "artech", "diverse lynx", "mindlance",
    "judge group", "akkodis", "cybercoders", "jobot", "motion recruitment",
    "ernst & young", "kpmg", "pwc", "pricewaterhouse", "booz",
)


def classify_company(company: str, startup_slugs: frozenset[str] | None = None) -> str:
    name = (company or "").lower()
    if any(c in name for c in CONSULTANCIES):
        return "consultancy"
    if startup_slugs and name in startup_slugs:
        return "startup"
    return "neutral"


@dataclass
class SkillGroup:
    name: str
    weight: float
    terms: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)


def _compile_term(term: str) -> re.Pattern[str]:
    """Word-boundary match, tolerant of the punctuation in terms like 'c++' or 'ci/cd'.

    Very short terms ("R", "Go") are matched case-sensitively. Case-insensitively,
    ``\\br\\b`` hits every stray "r" in a job description and floods the match list.
    """
    escaped = re.escape(term.strip())
    # \b behaves badly when the term starts or ends with a non-word character.
    left = r"\b" if term[:1].isalnum() else ""
    right = r"\b" if term[-1:].isalnum() else ""
    flags = 0 if len(term.strip()) <= 2 else re.I
    return re.compile(f"{left}{escaped}{right}", flags)


@dataclass
class Track:
    """One tailored resume and the vocabulary that signals a fit for it."""

    name: str
    label: str
    resume: str
    groups: list[SkillGroup]
    skill_target: float = 20.0
    # term (lowercased) -> (display name, weight, pattern), deduped across groups
    index: dict[str, tuple[str, float, re.Pattern[str]]] = field(default_factory=dict)

    def match(self, text: str) -> tuple[float, list[str]]:
        """Return (weighted score, matched term names). Each term counts once."""
        raw = 0.0
        matched: list[str] = []
        for display, weight, pattern in self.index.values():
            if pattern.search(text):
                raw += weight
                matched.append(display)
        return raw, matched

    def fraction(self, text: str) -> tuple[float, list[str]]:
        """Overlap as a 0-1 fraction of this track's target, plus the terms hit."""
        raw, matched = self.match(text)
        if not self.skill_target:
            return 0.0, matched
        return min(1.0, raw / self.skill_target), matched

    def core_terms(self) -> list[re.Pattern[str]]:
        """Compiled patterns for the groups that actually distinguish this track."""
        return [
            pattern
            for group in self.groups if group.name not in NON_CORE_GROUPS
            for _, pattern in group.terms
        ]


@dataclass
class Profile:
    """The four resumes, scored together."""

    tracks: list[Track]

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        tracks: list[Track] = []

        for name, spec in (data.get("tracks") or {}).items():
            groups: list[SkillGroup] = []
            for group_name, group_spec in (spec.get("skills") or {}).items():
                weight = float(group_spec.get("weight", 1.0))
                terms = [(t, _compile_term(t)) for t in (group_spec.get("terms") or []) if t]
                groups.append(SkillGroup(name=group_name, weight=weight, terms=terms))

            # A term listed in several groups must count once, at its highest weight.
            # Otherwise one technology quietly counts double toward the skill total.
            index: dict[str, tuple[str, float, re.Pattern[str]]] = {}
            for group in groups:
                for term, pattern in group.terms:
                    key = term.lower()
                    if key not in index or group.weight > index[key][1]:
                        index[key] = (term, group.weight, pattern)

            tracks.append(Track(
                name=name,
                label=str(spec.get("label") or name).strip(),
                resume=str(spec.get("resume") or "").strip(),
                groups=groups,
                skill_target=float(spec.get("skill_target", 20.0)),
                index=index,
            ))

        if not tracks:
            raise ValueError(f"{path} defines no tracks")
        return cls(tracks=tracks)

    def get(self, name: str) -> Track | None:
        for track in self.tracks:
            if track.name == name:
                return track
        return None

    def core_terms(self, exclude: tuple[str, ...] = ()) -> list[re.Pattern[str]]:
        """Core patterns across tracks, for the adjacency route in filters.

        ``exclude`` exists because of a measured mistake. That route asks "is this
        generic engineering posting secretly one of the roles I want", and admits on
        two distinct hits. Including the SWE track's own vocabulary in the pool makes
        the question circular: React plus pytest is two hits, so nearly any backend
        posting clears it. On a live sweep that admitted 354 postings through this
        route and left the stricter SWE-resume route contributing nothing at all,
        because the loose test always answered first. Excluding "swe" here keeps the
        two routes asking different questions.
        """
        return [
            p
            for track in self.tracks if track.name not in exclude
            for p in track.core_terms()
        ]

    def track_core_terms(self) -> dict[str, list[re.Pattern[str]]]:
        return {track.name: track.core_terms() for track in self.tracks}

    def best(self, text: str, family: str = "") -> tuple[Track, float, list[str]]:
        """Pick the track whose vocabulary the posting matches best.

        The job title gets a say through ``FAMILY_TRACK``: when the family's natural
        track is within ``TRACK_TITLE_MARGIN`` of the leader, it wins. A posting
        titled "Analytics Engineer" is answered with the Data resume even if its body
        happens to name one more backend term, because the title is what a reviewer
        screens against.
        """
        scored = [(track,) + track.fraction(text) for track in self.tracks]
        best_track, best_frac, best_matched = max(scored, key=lambda row: row[1])

        preferred = FAMILY_TRACK.get(family)
        if preferred and preferred != best_track.name:
            for track, frac, matched in scored:
                if track.name == preferred and best_frac - frac <= TRACK_TITLE_MARGIN:
                    return track, frac, matched
        return best_track, best_frac, best_matched


@dataclass
class Score:
    total: float
    family: float = 0.0
    skills: float = 0.0
    location: float = 0.0
    early: float = 0.0
    freshness: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    min_years: int | None = None
    company_kind: str = "neutral"
    # Which resume to send, and how the other three compared.
    track: str = ""
    track_label: str = ""
    resume: str = ""
    track_scores: dict[str, float] = field(default_factory=dict)

    def breakdown(self) -> str:
        return (
            f"family {self.family:.0f} | skills {self.skills:.0f} | "
            f"loc {self.location:.0f} | early {self.early:.0f} | "
            f"fresh {self.freshness:.0f}"
        )

    def track_summary(self) -> str:
        """"Data 78 · SWE 61 · AI 44 · FDE 30", strongest first."""
        ordered = sorted(self.track_scores.items(), key=lambda kv: -kv[1])
        return " · ".join(f"{name} {100 * frac:.0f}" for name, frac in ordered)


def score_job(
    job: Job,
    verdict: Verdict,
    profile: Profile,
    startup_slugs: frozenset[str] | None = None,
) -> Score:
    """Combine the gate's signals with resume overlap into a 0-100 score.

    Scores are normalized against the points *available* for a given posting rather
    than a fixed budget. Feed-sourced postings (Simplify, jobright) carry no
    description, so skill overlap is unmeasurable for them; charging them the full
    skills weight anyway pushed genuine "Data Engineering New Grad" roles below
    generic ones that merely had a long description to keyword-match against.
    """
    family_frac = verdict.family_weight
    location_frac = LOCATION_POINTS.get(verdict.location_tier, 0.25)

    # Experience fit, best evidence first: an explicit "new grad" marker, then the
    # stated years bar, then nothing known.
    #
    # New grad is emphasised but not required. With ~20 months of internships, 1-2
    # year roles are a fair match and 3 years is a reasonable stretch worth applying
    # to, so the curve tapers rather than falling off a cliff. Anything above 3 is
    # rejected outright by the seniority gate, not scored down.
    if verdict.early_signals:
        early_frac = 1.0
    elif verdict.min_years is None:
        early_frac = 0.50
    elif verdict.min_years <= 1:
        early_frac = 0.82
    elif verdict.min_years == 2:
        early_frac = 0.55
    else:
        early_frac = 0.28

    age = job.age_hours
    if not job.date_trusted:
        # The date is an index date, not a posting date, so it says nothing reliable
        # about freshness in either direction. Score it neutrally rather than
        # rewarding or punishing a number we know can be wrong by weeks.
        fresh_frac = 0.5
    elif age is None:
        fresh_frac = 0.25
    elif age <= 6:
        fresh_frac = 1.0
    elif age <= 24:
        fresh_frac = 0.8
    elif age <= 72:
        fresh_frac = 0.4
    else:
        fresh_frac = 0.0

    company_kind = classify_company(job.company, startup_slugs)
    company_frac = COMPANY_POINTS[company_kind]

    components: list[tuple[float, float]] = [
        (W_FAMILY, family_frac),
        (W_LOCATION, location_frac),
        (W_EARLY, early_frac),
        (W_COMPANY, company_frac),
        (W_FRESH, fresh_frac),
    ]

    # The distinction that matters is "has a body at all" - real descriptions run to
    # thousands of characters, while feed sources supply an empty string.
    has_description = len(job.description) >= 80
    track, skills_frac, matched = profile.best(job.haystack, verdict.family)
    track_scores = {t.label: t.fraction(job.haystack)[0] for t in profile.tracks}

    if has_description:
        components.append((W_SKILLS, skills_frac))
    else:
        # Title-only sources still reveal which resume fits, just far less reliably,
        # so the track is reported while the skill points are withheld.
        skills_frac = 0.0

    earned = sum(weight * frac for weight, frac in components)
    available = sum(weight for weight, _ in components)
    total = 100.0 * earned / available if available else 0.0

    # Slight haircut when we could not verify stack overlap, so an equally strong
    # fully-described posting outranks an unverified one.
    if not has_description:
        total *= 0.92

    scale = 100.0 / available if available else 0.0
    return Score(
        total=round(total, 1),
        family=W_FAMILY * family_frac * scale,
        skills=W_SKILLS * skills_frac * scale,
        location=W_LOCATION * location_frac * scale,
        early=W_EARLY * early_frac * scale,
        freshness=W_FRESH * fresh_frac * scale,
        matched_skills=matched,
        min_years=verdict.min_years,
        company_kind=company_kind,
        track=track.name,
        track_label=track.label,
        resume=track.resume,
        track_scores=track_scores,
    )
