"""Resume-match scoring.

Deliberately deterministic and dependency-free - no model calls on the hot path.
Latency is the entire point of this tool, and a keyword/weight model is both fast
enough to run on every poll and explainable, which matters because the Discord alert
shows *why* a role matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .filters import Verdict
from .models import Job

# Point budget, summing to 100 so thresholds read like percentages. Location carries
# real weight because SF / NYC / Toronto is a stated preference - without it a junior
# analyst role in a city he'd never move to outranked a strong Bay Area match.
W_FAMILY = 36.0
W_SKILLS = 32.0
W_LOCATION = 22.0
W_EARLY = 6.0
W_FRESH = 4.0

LOCATION_POINTS = {"tier1": 1.0, "tier2": 0.45, "unknown": 0.25, "reject": 0.0}


@dataclass
class SkillGroup:
    name: str
    weight: float
    terms: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)


@dataclass
class Score:
    total: float
    family: float = 0.0
    skills: float = 0.0
    location: float = 0.0
    early: float = 0.0
    freshness: float = 0.0
    matched_skills: list[str] = field(default_factory=list)

    def breakdown(self) -> str:
        return (
            f"family {self.family:.0f} | skills {self.skills:.0f} | "
            f"loc {self.location:.0f} | early {self.early:.0f} | "
            f"fresh {self.freshness:.0f}"
        )


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
class Profile:
    groups: list[SkillGroup]
    skill_target: float = 18.0
    # term (lowercased) -> (display name, weight, pattern), deduped across groups
    index: dict[str, tuple[str, float, re.Pattern[str]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        groups: list[SkillGroup] = []
        for name, spec in (data.get("skills") or {}).items():
            weight = float(spec.get("weight", 1.0))
            terms = [(t, _compile_term(t)) for t in (spec.get("terms") or []) if t]
            groups.append(SkillGroup(name=name, weight=weight, terms=terms))

        # A term listed in several groups (e.g. Databricks under both core_data and
        # platform) must count once, at its highest weight - otherwise one
        # technology quietly counts double toward the skill total.
        index: dict[str, tuple[str, float, re.Pattern[str]]] = {}
        for group in groups:
            for term, pattern in group.terms:
                key = term.lower()
                if key not in index or group.weight > index[key][1]:
                    index[key] = (term, group.weight, pattern)

        return cls(
            groups=groups,
            skill_target=float(data.get("skill_target", 18.0)),
            index=index,
        )

    def match(self, text: str) -> tuple[float, list[str]]:
        """Return (weighted score, matched term names). Each term counts once."""
        raw = 0.0
        matched: list[str] = []
        for display, weight, pattern in self.index.values():
            if pattern.search(text):
                raw += weight
                matched.append(display)
        return raw, matched


def score_job(job: Job, verdict: Verdict, profile: Profile) -> Score:
    """Combine the gate's signals with resume overlap into a 0-100 score.

    Scores are normalized against the points *available* for a given posting rather
    than a fixed budget. Feed-sourced postings (Simplify, SmartRecruiters) carry no
    description, so skill overlap is unmeasurable for them; charging them the full
    skills weight anyway pushed genuine "Data Engineering New Grad" roles below
    generic ones that merely had a long description to keyword-match against.
    """
    family_frac = verdict.family_weight
    location_frac = LOCATION_POINTS.get(verdict.location_tier, 0.25)

    early_frac = 1.0 if verdict.early_signals else 0.0
    # An explicit low experience bar is as good a signal as the words "new grad".
    if not early_frac and verdict.min_years is not None and verdict.min_years <= 1:
        early_frac = 0.75

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

    components: list[tuple[float, float]] = [
        (W_FAMILY, family_frac),
        (W_LOCATION, location_frac),
        (W_EARLY, early_frac),
        (W_FRESH, fresh_frac),
    ]

    raw_skills, matched = profile.match(job.haystack)
    # The distinction that matters is "has a body at all" - real descriptions run to
    # thousands of characters, while feed sources supply an empty string.
    has_description = len(job.description) >= 80
    if has_description and profile.skill_target:
        skills_frac = min(1.0, raw_skills / profile.skill_target)
        components.append((W_SKILLS, skills_frac))
    else:
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
    )
