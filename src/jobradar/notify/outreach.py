"""Turning an alert into a starting point for emailing a human.

Emailing recruiters and hiring leads directly is the highest-response channel in
this search, so the alert should hand over the next step rather than stopping at the
apply link.

What this does NOT do is scrape addresses, and that is a measured decision rather
than caution. Across 4,007 described postings from Greenhouse, Ashby and Lever,
949 email addresses appear in the body and **97.4% of them are accommodation or
accessibility addresses** - the line employers are legally required to publish so
candidates can request interview adjustments. Four occurrences were a generic
careers inbox and none were a named person. So an "extract the contact email"
feature would, almost every time, surface an ADA accommodation address and label it
a hiring contact. Emailing it would reach a compliance queue, not a recruiter.

Third-party contact databases would supply real addresses, but they hold personal
data gathered without the subject's involvement, and using them here would mean this
tool quietly building a file on people who never published an address at all. Not
worth it for a cold email that lands worse than a warm search does.

What works instead is handing over a search that lands on the right people, in the
place where they publish their own contactability. The links below are plain
searches, run by the user as themselves, resolving to real named people with their
current titles. Nothing is stored and no personal data passes through this tool.
"""

from __future__ import annotations

from urllib.parse import quote_plus

# Who to look for, per resume track. A recruiter is the reliable first contact, but
# the functional lead is the one who recognises the work, so both are offered.
LEAD_TITLE = {
    "data": "head of data",
    "ai": "head of AI",
    "fde": "head of solutions",
    "swe": "engineering manager",
}
DEFAULT_LEAD = "engineering manager"

PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/?keywords={query}"


def people_search(company: str, role: str) -> str:
    """A LinkedIn people search for a role at one company."""
    return PEOPLE_SEARCH.format(query=quote_plus(f"{company} {role}".strip()))


def outreach_field(company: str, track: str) -> dict[str, str | bool] | None:
    """An embed field with two searches: the recruiter, and the functional lead.

    Returns None when there is no company name to search on, which happens on a
    handful of feed rows.
    """
    name = (company or "").strip()
    if not name:
        return None

    lead = LEAD_TITLE.get(track, DEFAULT_LEAD)
    links = (
        f"[Recruiters]({people_search(name, 'recruiter talent acquisition')}) · "
        f"[{lead.title()}]({people_search(name, lead)})"
    )
    return {"name": "Who to email", "value": links[:1024], "inline": False}
