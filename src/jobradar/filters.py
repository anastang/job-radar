"""Hard gates that decide whether a posting is worth alerting on at all.

Design notes worth keeping in mind before editing:

* Title text alone is not sufficient. A live sample showed the strongest matches
  ("Analytics Engineer" at Linear, "Data Analyst" at Cloudflare) carry no seniority
  marker in the title, while the description says "5+ years". Seniority therefore
  comes from title patterns *and* years-of-experience parsed out of the body.
* The work-authorization rule is deliberately inverted from the usual one. The user
  is a Canadian citizen entering the US on TN status under USMCA, so "we do not offer
  sponsorship" is NOT a blocker - he answers yes to that question. Only citizenship,
  clearance, and ITAR requirements genuinely exclude him. Filtering on generic
  no-sponsorship language would silently discard most viable US roles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Job

# --------------------------------------------------------------------------------
# Role families
# --------------------------------------------------------------------------------

# Family weights are deliberately close together. The title is a relevance check, not
# the ranking signal - the whole point is to surface a variety of adjacent roles and
# let *skill and experience overlap* decide which ones he would be a strong candidate
# for. A data analyst posting that wants his exact stack should outrank a data
# engineer posting that shares almost nothing with his background.
ROLE_FAMILIES: list[tuple[str, float, re.Pattern[str]]] = [
    ("data_engineer", 1.00, re.compile(
        r"\bdata engineer(ing)?\b|\bdata platform engineer\b|\bdata infrastructure\b"
        r"|\bbig data engineer\b|\bdata pipeline engineer\b|\betl developer\b", re.I)),
    ("analytics_engineer", 0.97, re.compile(
        r"\banalytics engineer(ing)?\b", re.I)),
    ("ai_engineer", 0.93, re.compile(
        r"\b(ai|a\.i\.) engineer\b|\bapplied ai\b|\bgen(erative )?ai engineer\b"
        r"|\bllm engineer\b|\bapplied scientist\b|\bai/ml engineer\b", re.I)),
    ("data_analyst", 0.90, re.compile(
        r"\bdata analyst\b|\banalytics analyst\b|\bproduct analyst\b"
        r"|\bbusiness intelligence\b|\bbi (analyst|engineer|developer)\b", re.I)),
    ("data_scientist", 0.88, re.compile(
        r"\bdata scientist\b|\bdata science\b", re.I)),
    ("ml_engineer", 0.87, re.compile(
        r"\b(machine learning|ml) engineer\b", re.I)),
    ("forward_deployed", 0.85, re.compile(
        r"\bforward[- ]deployed\b", re.I)),
]

# Titles that contain a family keyword but are not the job he wants.
NEGATIVE_TITLE = re.compile(
    r"\bdata cent(er|re)\b|\bdata entry\b|\bclinical data\b|\bdata privacy\b"
    r"|\bdata protection\b|\bmaster data\b|\bsalesforce\b|\bworkday\b"
    r"|\bfinancial analyst\b|\bcredit analyst\b|\b(qa|quality|test) analyst\b"
    r"|\bsecurity analyst\b|\bsoc analyst\b|\bpolicy analyst\b"
    r"|\bsales analyst\b|\bmarketing analyst\b"
    # Teaching and QA roles that borrow data vocabulary without being data roles.
    r"|\binstructional\b|\bteaching assistant\b|\bcurriculum\b"
    r"|\bquality engineer\b|\bvalidation engineer\b|\brecruiter\b",
    re.I,
)

# --------------------------------------------------------------------------------
# Seniority
# --------------------------------------------------------------------------------

SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|leader|manager|mgr|director|head of"
    r"|vp|vice president|architect|chief|distinguished|fellow|expert)\b",
    re.I,
)

# Level III and up. "I" and "II" are left alone - those are still early career.
SENIOR_ROMAN = re.compile(r"\b(iii|iv|vi{0,3}|ix|x)\b", re.I)

INTERNSHIP_TITLE = re.compile(
    r"\bintern\b|\binternship\b|\bco[- ]?op\b|\bapprentice(ship)?\b"
    r"|\bworking student\b|\bphd\b|\bsummer 20\d\d\b",
    re.I,
)

EARLY_SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("new_grad", re.compile(r"\bnew[- ]grad(uate)?\b|\buniversity grad(uate)?\b"
                            r"|\brecent grad(uate)?\b|\bcampus hire\b", re.I)),
    ("entry_level", re.compile(r"\bentry[- ]level\b|\bearly career\b", re.I)),
    ("junior", re.compile(r"\bjunior\b|\bjr\.?\b|\bassociate\b|\blevel 1\b", re.I)),
    ("grad_year", re.compile(r"\b20(25|26|27) grad\b|\bclass of 20(25|26|27)\b", re.I)),
]

# "5+ years", "3-5 years", "at least 2 yrs". The trailing lookahead keeps us from
# matching things like "20 years of company history".
YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.I,
)

# Deciding whether "N years" is a requirement takes two passes, because neither test
# works alone:
#
#   * Requiring the word "experience" nearby missed "4-8+ years in data or analytics
#     engineering" and "2-5+ years working as a data engineer", so senior roles went
#     unparsed and sailed through the gate.
#   * Accepting every mention then picked up benefits text - Linear's "Paid month off
#     after 4 years & every 2 years thereafter" made a 5-year role look like a
#     2-year one, since the minimum wins.
#
# The distinguishing feature is direction: requirement language *follows* the number
# ("5+ years of experience in..."), while benefits and company prose *precede* it
# ("paid month off after 4 years"). So a positive match immediately after the number
# wins outright, and only otherwise do we look around for disqualifying context.
IS_A_REQUIREMENT = re.compile(
    r"^\W{0,4}(of |in |as |with )?(experien|working|work\b|professional|hands[- ]on"
    r"|relevant|industry|background|track record|building|built|developing|designing"
    r"|in (data|analytics|software|engineering|ml|machine|a |an |the )"
    r"|(as|in) an?\b|role\b|equivalent)",
    re.I,
)
NOT_A_REQUIREMENT = re.compile(
    # Company prose.
    r"\bago\b|\bhistory\b|founded|\bin business\b|anniversary|since \d{4}"
    r"|over the (past|last)|for (more than |over )?\d+ years we|celebrat"
    # Benefits and tenure. Linear's posting reads "Paid month off after 4 years &
    # every 2 years thereafter" - counting those made a role that genuinely wants
    # 5+ years look like a 2-year one, because the minimum wins.
    r"|parental leave|sabbatical|paid (month|time|leave)|month off|\bpto\b"
    r"|vacation|vesting|thereafter|every \d+ years?|401\(?k\)?|tenure"
    r"|\bperks?\b|\bbenefits?\b|holiday|equity refresh|stock option",
    re.I,
)

# --------------------------------------------------------------------------------
# Work authorization - see module docstring for why this list is short
# --------------------------------------------------------------------------------

AUTH_BLOCKERS: list[tuple[str, re.Pattern[str]]] = [
    ("us_citizenship", re.compile(
        r"u\.?s\.?\s*citizenship\s*(is\s*)?(required|a requirement)"
        r"|must be a (u\.?s\.?|united states) citizen"
        r"|u\.?s\.? citizens? only", re.I)),
    ("clearance", re.compile(
        r"security clearance|ts/sci|top secret|secret clearance"
        r"|active clearance|polygraph|public trust", re.I)),
    # "export control" on its own is boilerplate - Cloudflare and many other ordinary
    # US employers attach an Export Administration Regulations notice to every
    # posting. It implies nothing about citizenship, so match only the phrasings that
    # actually restrict who may hold the role.
    ("itar", re.compile(
        r"\bitar\b|u\.?s\.? person(s)? only|must be a u\.?s\.? person"
        r"|u\.?s\.? person status (is )?required", re.I)),
    ("green_card", re.compile(r"green card (holder )?(is )?required", re.I)),
]

# --------------------------------------------------------------------------------
# Location
# --------------------------------------------------------------------------------

TIER1_LOC = re.compile(
    r"san francisco|bay area|silicon valley|\bsf\b|palo alto|mountain view|menlo park"
    r"|new york|\bnyc\b|manhattan|brooklyn"
    # Greater Toronto Area - suburb names are how postings usually spell it.
    r"|toronto|\bgta\b|mississauga|brampton|markham|vaughan|etobicoke|scarborough"
    r"|north york|richmond hill|oakville|burlington, on|waterloo|kitchener"
    # Major North American tech hubs, weighted equally with the preferred three:
    # relocating for the right role is on the table, so a Seattle posting should not
    # sit twelve points behind an identical one in SF.
    r"|seattle|bellevue|redmond|austin|boston|cambridge, ma|denver|boulder"
    r"|vancouver|montreal|montr|ottawa|calgary|san jose|santa clara|sunnyvale"
    r"|chicago|los angeles|san diego|portland|atlanta|dallas|washington, dc",
    re.I,
)

# Roles that borrow data vocabulary but are ordinary software engineering. Accepted
# only when the posting itself is clearly data/AI-centric - see evaluate().
SWE_TITLE = re.compile(
    r"\bsoftware (engineer|developer)\b|\bswe\b|\bbackend engineer\b"
    r"|\bplatform engineer\b|\bfull[- ]stack engineer\b",
    re.I,
)

# Evidence that a generic SWE posting is really a data/AI job.
DATA_CONTEXT = re.compile(
    r"\bdata pipeline|\betl\b|\belt\b|\bdata warehouse|\bspark\b|\bkafka\b|\bairflow\b"
    r"|\bdbt\b|\bsnowflake\b|\bredshift\b|\bbigquery\b|\bdatabricks\b|\bflink\b"
    r"|machine learning|\bllm\b|\bml (platform|infra)|data infrastructure"
    r"|data platform|streaming|analytics",
    re.I,
)
DATA_CONTEXT_MIN_HITS = 3

CANADA_HINT = re.compile(r"canada|ontario|,\s*on\b|,\s*bc\b|,\s*ab\b|,\s*qc\b", re.I)

TIER2_LOC = re.compile(
    r"remote|north america|united states|\busa\b|\bu\.?s\.?a?\b|canada|anywhere"
    r"|seattle|austin|boston|chicago|los angeles|san jose|santa clara|sunnyvale"
    r"|denver|atlanta|vancouver|montreal|ottawa|calgary|washington|philadelphia"
    r"|san diego|portland|miami|dallas|houston|phoenix|minneapolis|pittsburgh"
    r"|raleigh|nashville|detroit|salt lake|boulder|irvine|bellevue",
    re.I,
)

NON_NA_LOC = re.compile(
    r"ireland|dublin|france|paris|united kingdom|\buk\b|london|germany|berlin|munich"
    r"|india|bangalore|bengaluru|hyderabad|gurgaon|pune|singapore|australia|sydney"
    r"|melbourne|japan|tokyo|brazil|s(a|ã)o paulo|poland|warsaw|krakow"
    r"|netherlands|amsterdam|spain|madrid|barcelona|portugal|lisbon|israel|tel aviv"
    r"|china|shanghai|beijing|hong kong|korea|seoul|philippines|manila|argentina"
    r"|colombia|bogot|chile|italy|milan|sweden|stockholm|switzerland|zurich|denmark"
    r"|copenhagen|norway|oslo|finland|helsinki|austria|vienna|belgium|brussels"
    r"|czech|prague|romania|bucharest|ukraine|turkey|istanbul|\buae\b|dubai"
    r"|south africa|new zealand|taiwan|vietnam|thailand|bangkok|indonesia|jakarta"
    r"|malaysia|kuala lumpur|kenya|nigeria|egypt|mexico city|guadalajara|costa rica"
    # Added after Workday postings in Lima and Bangalore reached the alerts.
    r"|\bperu\b|\blima\b|ecuador|uruguay|paraguay|bolivia|venezuela|panama"
    r"|hyderabad|telangana|karnataka|chennai|mumbai|delhi|noida|kolkata"
    r"|greece|athens|hungary|budapest|bulgaria|sofia|serbia|belgrade|croatia"
    r"|slovakia|slovenia|lithuania|latvia|estonia|morocco|tunisia|ghana|rwanda"
    r"|pakistan|bangladesh|sri lanka|nepal|saudi|qatar|kuwait|bahrain|oman",
    re.I,
)


@dataclass
class Verdict:
    """Outcome of the hard-gate pass, plus signals scoring will reuse."""

    passed: bool
    reason: str = ""
    family: str = ""
    family_weight: float = 0.0
    location_tier: str = "unknown"
    min_years: int | None = None
    early_signals: list[str] = field(default_factory=list)


def classify_role(title: str) -> tuple[str, float] | None:
    if NEGATIVE_TITLE.search(title):
        return None
    for name, weight, pattern in ROLE_FAMILIES:
        if pattern.search(title):
            return name, weight
    return None


def min_years_required(text: str, cap: int = 20) -> int | None:
    """Smallest years-of-experience requirement stated anywhere in the posting.

    The minimum is the right statistic: postings routinely pair a baseline ("2+
    years required") with a higher preferred bar ("5+ years preferred"). Taking the
    max would reject roles he qualifies for.
    """
    if not text:
        return None
    found: list[int] = []
    for match in YEARS_RE.finditer(text):
        after = text[match.end(): match.end() + 70]
        if not IS_A_REQUIREMENT.match(after):
            # No requirement language follows, so look around for the tell-tale
            # benefits or company-prose wording before discarding it.
            context = text[max(0, match.start() - 70): match.end() + 70]
            if NOT_A_REQUIREMENT.search(context):
                continue
        try:
            value = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 0 <= value <= cap:
            found.append(value)
    return min(found) if found else None


def auth_blocker(text: str) -> str | None:
    for name, pattern in AUTH_BLOCKERS:
        if pattern.search(text):
            return name
    return None


def classify_location(text: str) -> str:
    """tier1 / tier2 / unknown / reject.

    Order matters. A multi-location posting like "New York; London" must resolve to
    tier1, so North American signals are checked before the non-NA reject list.
    """
    if not text.strip():
        return "unknown"
    if TIER1_LOC.search(text):
        return "tier1"
    if CANADA_HINT.search(text):
        return "tier2"
    if NON_NA_LOC.search(text):
        return "reject"
    if TIER2_LOC.search(text):
        return "tier2"
    return "unknown"


def early_career_signals(text: str) -> list[str]:
    return [name for name, pattern in EARLY_SIGNALS if pattern.search(text)]


def evaluate(job: Job, cfg: dict | None = None) -> Verdict:
    """Apply every hard gate. Returns a Verdict carrying signals for scoring."""
    cfg = cfg or {}
    max_years = int(cfg.get("max_years_experience", 3))
    allow_internships = bool(cfg.get("allow_internships", False))
    allow_unknown_location = bool(cfg.get("allow_unknown_location", True))

    title = job.title
    if not title:
        return Verdict(False, "no_title")

    # 0. Employers whose roles are effectively closed to a Canadian citizen. Cleared
    #    defense work states its clearance requirement in the description, which the
    #    auth gate below catches - but feed-sourced postings carry no description, so
    #    these leak through on title alone. Blocking by employer closes that hole.
    blocked = [b.lower() for b in (cfg.get("blocked_companies") or [])]
    company = job.company.lower()
    if any(b in company for b in blocked):
        return Verdict(False, "blocked_company")

    # 1. Role family
    role = classify_role(title)
    if role is None:
        # Generic software engineering counts only when the posting is genuinely
        # data/AI-centric. A Spring Boot / Docker / Kubernetes internship makes these
        # a fair target, but accepting every "Software Engineer" title outright would
        # bury the data roles under generic web-dev postings.
        if (
            cfg.get("allow_data_adjacent_swe")
            and SWE_TITLE.search(title)
            and not NEGATIVE_TITLE.search(title)
            and len(set(m.group(0).lower() for m in DATA_CONTEXT.finditer(job.haystack)))
            >= DATA_CONTEXT_MIN_HITS
        ):
            family, weight = "software_engineer", 0.85
        else:
            return Verdict(False, "role_family")
    else:
        family, weight = role

    # 1b. Stale postings. The point of this tool is being early, so a posting that
    #     has been live for weeks is not worth an alert even when newly *seen*.
    #     This guards two real cases: adding a company dumps its whole back
    #     catalogue into one run, and the 90-day state prune can make a long-lived
    #     posting look new again. Postings with no date are allowed through -
    #     unknown must not mean rejected.
    #     Only applied to sources that report the employer's real posting date. The
    #     community feed reports its own indexing date and never refreshes it, so a
    #     job posted yesterday can carry a 40-day-old timestamp - gating on that
    #     would silently discard exactly the fresh roles this tool exists to catch.
    max_age_days = cfg.get("max_age_days")
    if max_age_days and job.date_trusted:
        age = job.age_hours
        if age is not None and age > float(max_age_days) * 24:
            return Verdict(False, "stale")

    # 2. Internships / co-ops (he has already graduated)
    if not allow_internships and INTERNSHIP_TITLE.search(title):
        return Verdict(False, "internship", family=family, family_weight=weight)

    # 3. Seniority by title
    if SENIOR_TITLE.search(title) or SENIOR_ROMAN.search(title):
        return Verdict(False, "senior_title", family=family, family_weight=weight)

    # Structured level, where the source provides one (SmartRecruiters).
    if job.extra.get("blocking_level"):
        return Verdict(False, "senior_level", family=family, family_weight=weight)

    # 4. Seniority by stated experience. A missing description means "unknown",
    #    which must not be treated as a rejection - several sources omit the body.
    years = min_years_required(job.description)
    if years is not None and years > max_years:
        return Verdict(False, f"years_{years}", family=family, family_weight=weight,
                       min_years=years)

    # 5. Work authorization
    if job.extra.get("blocking_sponsorship"):
        return Verdict(False, "auth_sponsorship", family=family, family_weight=weight)
    blocker = auth_blocker(job.haystack)
    if blocker:
        return Verdict(False, f"auth_{blocker}", family=family, family_weight=weight,
                       min_years=years)

    # 6. Location
    tier = classify_location(job.location_raw or job.description[:400])
    if tier == "reject":
        return Verdict(False, "location", family=family, family_weight=weight,
                       min_years=years)
    if tier == "unknown" and not allow_unknown_location:
        return Verdict(False, "location_unknown", family=family, family_weight=weight)

    return Verdict(
        passed=True,
        family=family,
        family_weight=weight,
        location_tier=tier,
        min_years=years,
        early_signals=early_career_signals(f"{title}\n{job.description[:2000]}"),
    )
