# Job Radar

Monitors ~170 company job boards for early-career **data engineering / analytics
engineering / data analyst / AI engineering** roles and pushes a Discord alert within
minutes of a posting going live.

The whole design is built around one constraint: **being early**. New-grad data roles
collect hundreds of applicants within a day, so the goal is not a list of jobs — it is
a notification while the posting is still fresh.

## Why it polls ATS APIs instead of scraping LinkedIn

Company applicant-tracking boards (Greenhouse, Ashby, Lever, SmartRecruiters) are where
a posting *originates*. LinkedIn and Indeed are downstream mirrors that lag by hours to
days, and scraping them violates their terms and invites IP bans and CAPTCHAs.

Polling the ATS JSON APIs directly is both faster and legitimate — these are the same
public endpoints that render each company's own careers page. **This tool does not
scrape LinkedIn or Indeed**, by design.

| Source | Freshness field | Date trusted? | Description text |
|---|---|---|---|
| Greenhouse | `first_published` — true publish time | yes | yes (`?content=true`) |
| Ashby | `publishedAt` | yes | yes, plus compensation |
| Lever | `createdAt` (epoch ms) | yes | yes, plus salary range |
| Workable | `published_on` (date-granular) | yes | yes (`?details=true`) |
| SmartRecruiters | `releasedDate` | yes | no — gives `experienceLevel` |
| Workday | `postedOn` — prose, "Posted 13 Days Ago" | yes, coarse | no |
| SimplifyJobs new-grad feed | `date_posted` | **no — index date** | no — breadth backstop |

Roughly 600 boards across six providers, including ~350 YC startups.

### The feed's dates are not posting dates

The community feed records when *it* indexed a job and never refreshes that when the
employer re-posts. A real case: EXL's "Data Engineer" carried `date_posted`
2026-07-08 while the employer's own page said 2026-08-16 — off by 39 days.

Every other source reads the employer's own system, so only the feed is affected.
Because a wrong date here would make a day-old job look month-old, feed-sourced
postings are exempt from the staleness gate, score neutrally on freshness rather than
being penalized, and are labelled "Listed … (feed index — check listing)" in Discord
instead of claiming a posting date. See `UNTRUSTED_DATE_SOURCES` in `models.py`.

## Setup

Install the package itself (not just the requirements) so `jobradar` is on PATH and
no `PYTHONPATH` juggling is needed:

```bash
pip install -e .
```

**1. Create a Discord webhook.** Server Settings → Integrations → Webhooks → New
Webhook, copy the URL. Test it — `--webhook` avoids environment-variable syntax
differences between shells:

```bash
python -m jobradar.notify.discord --test --webhook "https://discord.com/api/webhooks/..."
```

**2. Bootstrap.** The first run records everything currently posted *without* alerting,
so you don't get several hundred notifications at once:

```bash
jobradar
```

### Shell note (Windows / PowerShell)

`VAR=value command` is bash syntax and fails in PowerShell with
*"is not recognized as the name of a cmdlet"*. Set the variable first:

```bash
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
```

Then run `jobradar` normally. The examples below work unchanged in both shells.

**3. Run it continuously.** Push to a **private** GitHub repo and add
`DISCORD_WEBHOOK_URL` under Settings → Secrets and variables → Actions. The workflow in
`.github/workflows/poll.yml` then polls every 5 minutes and commits `state/seen.json`
back so dedupe survives between runs.

> Keep the repo private: `config/profile.yaml` describes your background. The resume
> itself is never committed — `.gitignore` excludes PDFs.

## Usage

```bash
jobradar --dry-run --since 7 --all-tiers
```

| Flag | Effect |
|---|---|
| `--dry-run` | print matches; never notify or write state |
| `--since N` | only consider postings from the last N days |
| `--all-tiers` | poll tier-2 companies and the community feed regardless of cadence |
| `--min-score N` | override the notify threshold (use `0` to see everything) |
| `--bootstrap` | record everything as seen without alerting |
| `--notify-on-cold-start` | alert on the very first run instead of bootstrapping |

Review the current backlog at any time:

```bash
jobradar --dry-run --since 14 --min-score 0
```

## How filtering works

A posting must clear every hard gate before it is scored:

1. **Role family** — data engineer / analytics engineer / data analyst / AI engineer,
   plus data scientist, BI, ML engineer and forward-deployed roles.
2. **Seniority** — senior/staff/principal/lead/manager titles and level III+ are out.
   Then the *description* is parsed for a years-of-experience requirement, because the
   strongest matches often carry no seniority marker in the title at all. Anything
   above `max_years_experience` (default 3) is rejected.
3. **Work authorization** — see below.
4. **Location** — SF / NYC / Toronto score highest, other North America lower,
   non-North-America is rejected unless the posting also lists a target city.
5. **Internships** are excluded by default.

Survivors are scored out of 100:

| Component | Points | Notes |
|---|---|---|
| **Resume skill overlap** | **38** | weighted terms from `profile.yaml` — the dominant term |
| Role family | 20 | 1.00 down to 0.85; a relevance check, not the ranking signal |
| Location | 20 | major NA hubs 1.0, remote/elsewhere 0.65, unknown 0.35 |
| Experience fit | 12 | new grad 1.0, 1 yr 0.82, 2 yrs 0.55, 3 yrs 0.28 |
| Company character | 6 | startup 1.0, neutral 0.6, consultancy 0.0 |
| Freshness | 4 | under 6h full marks, decaying to zero at 3 days |

**Skill overlap dominates on purpose.** The title only establishes that a role is
relevant at all; what makes him a strong *candidate* is how much of the posting's
stack he has actually shipped. Family weights sit deliberately close together
(1.00–0.85) so a variety of adjacent roles stay in play. In practice a data analyst
posting asking for dbt, Airflow and Spark outranks a data engineer posting that
shares almost nothing with his background — which is the intended behaviour.

Postings without a description are normalized against the points actually available
rather than being penalized for text they never had — otherwise a real "Data
Engineering New Grad" ranks below a generic role that merely had more text to
keyword-match.

### Tuning it to your preferences

The scoring is built to **rank rather than gate**: being slightly underqualified does
not mean no chance, so `notify_min` sits low (48) and the score carries the signal
about which roles are worth dropping everything for. Raise the threshold if volume
gets tiring — that is the cleanest dial. The knobs that shape *what* gets surfaced:

- **`max_years_experience`** (3) — the hard ceiling. 4+ year roles are rejected
  outright; 1–3 year roles rank below new-grad ones but still alert.
- **`allow_data_adjacent_swe`** (true) — counts generic "Software Engineer" titles
  when the body carries at least three distinct data/AI signals. Widens the pool
  without admitting every web-dev posting.
- **`blocked_companies`** — never alert at all. Separate from consultancies, which
  are scored down rather than excluded.
- **`CONSULTANCIES`** in `scoring.py` — staffing and IT-services firms that post high
  volumes of data-titled placement roles. They still alert, six points lower.
- **Startup boost** — anything in `companies_yc.yaml` counts as a startup, since that
  file is by construction actively-hiring YC companies.

### Work authorization is deliberately non-standard

Configured for a **Canadian citizen entering the US on TN status** under USMCA. TN is
not sponsorship in the H-1B sense, so:

- "We do not offer sponsorship" / "must be authorized to work in the US without
  sponsorship" — **not treated as a blocker**. Filtering on that language would discard
  most viable US roles.
- US citizenship requirements, security clearance, and ITAR restrictions **are**
  blockers, along with a list of cleared defense employers in `config/config.yaml`.

Note that generic Export Administration Regulations boilerplate appears on many
ordinary US postings and is explicitly *not* treated as a restriction — there is a
regression test covering this.

## Configuration

| File | Purpose |
|---|---|
| `config/config.yaml` | thresholds, cadence, filters, blocked employers |
| `config/companies.yaml` | generated — hand-seeded companies by ATS and tier |
| `config/companies_yc.yaml` | generated — YC startups that are actively hiring |
| `config/companies_feed.yaml` | generated — Workday/Workable boards harvested from the feed |
| `config/profile.yaml` | generated — resume-derived skill terms and weights |

Every `config/companies*.yaml` file is merged at runtime. They are kept separate so
that re-running one generator cannot wipe out another's boards. A slug listed as
tier1 anywhere wins over a tier2 listing elsewhere.

Regenerate periodically — boards migrate between ATS vendors and startups come and go:

```bash
python scripts/validate_companies.py
```

```bash
python scripts/discover_yc.py
```

```bash
python scripts/discover_from_feed.py
```

### Why three generators

`validate_companies.py` probes a hand-maintained list of established companies.
`discover_yc.py` walks the public YC directory, keeps companies that are active,
hiring, and in the target metros, and guesses their ATS slug from name and domain —
about a third resolve. `discover_from_feed.py` harvests Workday and Workable boards
from real posting URLs in the community feed, because Workday boards are addressed by
host *and* tenant *and* site (`ngc.wd1` / `ngc` / `Northrop_Grumman_External_Site`)
and cannot be guessed. Blocked employers are dropped at discovery time rather than at
filter time — there is no point spending a poll on a board whose every posting would
be rejected.

Everything discovered lands in tier2. Hundreds of startup boards polled every five
minutes would be inconsiderate to the ATS providers for little gain; a 30-minute
cadence is still far ahead of anyone browsing a job board.

Regenerate the skill profile whenever the resume changes:

```bash
python scripts/build_profile.py --resume "path/to/resume.pdf"
```

## Tests

```bash
python -m pytest
```

`-m "not live"` skips the two tests that hit real endpoints. One of those is a
regression guard worth keeping: **Ashby returns 404 for every request without a
browser User-Agent** — silently, and for the entire source.

## Tuning

If you get too many alerts, raise `scoring.notify_min`. Too few, lower it — postings
below the threshold are still recorded in `state/seen.json` with their score, so
`--dry-run --min-score 0` shows what you're missing and where to set the bar.
