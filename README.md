# Job Radar

[![Tests](https://github.com/anastang/job_scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/anastang/job_scraper/actions/workflows/tests.yml)

Job Radar watches about 570 company job boards for early-career **data engineering,
analytics engineering, data analyst, and AI engineering** roles, and sends a Discord
alert within minutes of a posting going live.

Speed is the point. New-grad data roles collect hundreds of applicants within a day,
so the tool is built to notify while a posting is still fresh rather than to produce a
searchable list.

## Why it polls ATS APIs instead of scraping LinkedIn

Company applicant-tracking boards are where a posting originates. LinkedIn and Indeed
mirror those boards hours or days later, and scraping them breaks their terms of
service and leads to IP bans and CAPTCHAs.

The ATS JSON APIs are faster, and they are public: they are the same endpoints that
render each company's own careers page. **Job Radar does not scrape LinkedIn or
Indeed.**

| Source | Freshness field | Date trusted? | Description text |
|---|---|---|---|
| Greenhouse | `first_published`, the true publish time | yes | yes (`?content=true`) |
| Ashby | `publishedAt` | yes | yes, plus compensation |
| Lever | `createdAt` (epoch ms) | yes | yes, plus salary range |
| Workable | `published_on` (date granularity) | yes | yes (`?details=true`) |
| SmartRecruiters | `releasedDate` | yes | no, but gives `experienceLevel` |
| Workday | `postedOn`, prose like "Posted 13 Days Ago" | coarse | no |
| SimplifyJobs new-grad feed | `date_posted` | **no, it is an index date** | no |

That covers six ATS providers plus one community feed, including roughly 350 YC
startups.

### The feed reports index dates, not posting dates

The SimplifyJobs feed records when it first indexed a job, and it does not refresh that
value when an employer re-posts. One example: the EXL "Data Engineer" listing carried a
`date_posted` of 2026-07-08 while the employer's own page said 2026-08-16, a gap of 39
days.

Every other source reads the employer's own system, so the problem is limited to the
feed. A stale date there would make a day-old job look a month old, so feed postings
are exempt from the staleness gate, score neutrally on freshness, and appear in Discord
labelled "Listed ... (feed index, check listing)" rather than claiming a posting date.
See `UNTRUSTED_DATE_SOURCES` in `models.py`.

## Setup

Install the package so that `jobradar` lands on your PATH:

```bash
pip install -e .
```

**1. Create a Discord webhook.** Server Settings, then Integrations, then Webhooks,
then New Webhook. Copy the URL and test it. Passing `--webhook` avoids the
environment-variable syntax differences between shells:

```bash
python -m jobradar.notify.discord --test --webhook "https://discord.com/api/webhooks/..."
```

**2. Bootstrap.** The first run records every posting that already exists without
alerting on any of it, so from that point on you hear only about new openings:

```bash
jobradar
```

**3. Run it continuously.** Push to GitHub and add two repository secrets under
Settings, then Secrets and variables, then Actions:

| Secret | Value |
|---|---|
| `DISCORD_WEBHOOK_URL` | the webhook you tested above |
| `DISCORD_MENTION` | your Discord user ID, so scores of 75 and above ping your phone |

`.github/workflows/poll.yml` then polls every 5 minutes and commits `state/seen.json`
back to the repo, so deduplication survives between runs.

Nothing sensitive is committed. The resume is gitignored, `config/profile.yaml`
contains only technology names, and `state/seen.json` stores opaque hashes instead of
the roles it surfaced.

### Shell note for Windows and PowerShell

`VAR=value command` is bash syntax. In PowerShell it fails with "is not recognized as
the name of a cmdlet". Set the variable first:

```bash
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
```

Then run `jobradar` as normal. Every other example works in both shells.

### Running on a private repo

GitHub Actions bills each job rounded up to a full minute, so one poll costs one minute
however fast it runs. The free allowance for private repos is 2,000 minutes per month,
which caps you at 2,000 polls. A 5-minute schedule needs 8,640 and exhausts the month
in about a week. Public repos have unlimited minutes.

To stay private, pick one of these:

- Reduce the cron to roughly `*/10 12-23 * * 1-5` and set `tier2_every_minutes: 30` in
  `config/config.yaml`. Measured against 2,962 real postings, 77.7% publish inside that
  window and 2.3% land on a weekend.
- Run the poller elsewhere. An always-free cloud VM works with no code changes, since
  it runs the same command CI runs.

## Usage

```bash
jobradar --dry-run --since 7
```

| Flag | Effect |
|---|---|
| `--dry-run` | print matches, never notify or write state |
| `--since N` | only consider postings from the last N days |
| `--min-score N` | override the notify threshold (use `0` to see everything) |
| `--limit N` | cap how many rows the dry-run table prints |
| `--all-tiers` | poll every board regardless of cadence settings |
| `--bootstrap` | record everything as seen without alerting |
| `--notify-on-cold-start` | alert on the first run instead of bootstrapping |

To review what the tool currently sees:

```bash
jobradar --dry-run --since 14 --min-score 0
```

## How filtering works

A posting must clear every gate before it is scored:

1. **Role family.** Data engineer, analytics engineer, data analyst, AI engineer, plus
   data scientist, BI, ML engineer, and forward-deployed roles.
2. **Seniority.** Senior, staff, principal, lead, and manager titles are rejected, as
   is level III and above. The description is then parsed for a years-of-experience
   requirement, because the strongest matches often carry no seniority marker in the
   title. Anything above `max_years_experience` (default 3) is rejected.
3. **Work authorization.** See below.
4. **Location.** Major North American tech hubs score equally. Remote and smaller
   cities score lower. Postings outside North America are rejected unless they also
   list a hub.
5. **Internships and co-ops** are excluded by default.

Survivors are scored out of 100:

| Component | Points | Notes |
|---|---|---|
| **Resume skill overlap** | **38** | weighted terms from `profile.yaml`, the largest component |
| Role family | 20 | 1.00 down to 0.85, a relevance check rather than a ranking signal |
| Location | 20 | major NA hubs 1.0, remote and elsewhere 0.65, unknown 0.35 |
| Experience fit | 12 | new grad 1.0, 1 year 0.82, 2 years 0.55, 3 years 0.28 |
| Company character | 6 | startup 1.0, neutral 0.6, consultancy 0.0 |
| Freshness | 4 | full marks under 6 hours, decaying to zero at 3 days |

**Skill overlap carries the most weight.** A job title establishes only that a role is
relevant. What makes someone a strong candidate is how much of the posting's stack they
have shipped. Family weights sit close together, from 1.00 to 0.85, so that a range of
adjacent roles stay in play. A data analyst posting asking for dbt, Airflow, and Spark
will outrank a data engineer posting that shares little with the profile.

Postings without a description are scored against the points available to them rather
than charged for text they never had. Otherwise a genuine "Data Engineering New Grad"
listing would rank below a generic role that happened to carry more keywords.

### Tuning

The scoring ranks rather than gates. Someone slightly underqualified still has a
chance, so `notify_min` sits low at 48 and the score itself signals which roles deserve
immediate attention. Raise the threshold if the volume becomes tiring, which is the
simplest dial to turn.

The settings that shape what gets surfaced:

- **`max_years_experience`** (3). The hard ceiling. Roles asking for 4 or more years
  are rejected. Roles asking for 1 to 3 rank below new-grad postings but still alert.
- **`allow_data_adjacent_swe`** (true). Counts generic "Software Engineer" titles when
  the body carries at least three distinct data or AI signals. This widens the pool
  while keeping out general web development postings.
- **`blocked_companies`.** Never alert at all. Separate from consultancies, which are
  scored down instead of excluded.
- **`CONSULTANCIES`** in `scoring.py`. Staffing and IT services firms that post large
  volumes of data-titled placement roles. They still alert, six points lower.
- **Startup boost.** Anything in `companies_yc.yaml` counts as a startup, since that
  file contains actively hiring YC companies by construction.

### Work authorization

Configured for **TN status under USMCA**. TN differs from H-1B sponsorship, which
inverts the usual filter:

- "We do not offer sponsorship" and "must be authorized to work in the US without
  sponsorship" **pass the filter**. Rejecting that language would discard most viable
  US roles.
- US citizenship requirements, security clearance, and ITAR restrictions **are
  blockers**, as are the cleared defense employers listed in `config/config.yaml`.

Generic Export Administration Regulations boilerplate appears on many ordinary US
postings and passes the filter. A regression test covers this.

## Configuration

| File | Purpose |
|---|---|
| `config/config.yaml` | thresholds, cadence, filters, blocked employers |
| `config/companies.yaml` | generated, hand-seeded companies by ATS and tier |
| `config/companies_yc.yaml` | generated, YC startups that are actively hiring |
| `config/companies_feed.yaml` | generated, Workday and Workable boards from the feed |
| `config/profile.yaml` | generated, resume-derived skill terms and weights |

Every `config/companies*.yaml` file is merged at runtime. They stay separate so that
re-running one generator leaves the others alone. A slug listed as tier1 anywhere wins
over a tier2 listing elsewhere.

`tier2_every_minutes` and `simplify_every_minutes` default to 0, meaning every board is
polled on every run. Tiering exists for private repos, where Actions minutes are capped
and the cadence has to be spread out.

Regenerate the company lists periodically, since boards migrate between ATS vendors and
startups appear and disappear:

```bash
python scripts/validate_companies.py
```

```bash
python scripts/discover_yc.py
```

```bash
python scripts/discover_from_feed.py
```

### Why there are three generators

`validate_companies.py` probes a hand-maintained list of established companies.

`discover_yc.py` walks the public YC directory, keeps companies that are active,
hiring, and in the target metros, and guesses each ATS slug from the company name and
website domain. About a third resolve.

`discover_from_feed.py` harvests Workday and Workable boards from real posting URLs in
the community feed. Workday boards are addressed by host, tenant, and site together
(`ngc.wd1` / `ngc` / `Northrop_Grumman_External_Site`), which makes them impossible to
guess. Blocked employers are dropped during discovery rather than at filter time, so no
poll is spent on a board whose every posting would be rejected.

Regenerate the skill profile whenever the resume changes:

```bash
python scripts/build_profile.py --resume "path/to/resume.pdf"
```

## Tests

```bash
python -m pytest
```

Add `-m "not live"` to skip the two tests that hit real endpoints, which is what CI
does. Keep them for local runs. One guards a requirement that is easy to reintroduce:
**Ashby returns 404 for every request that arrives without a browser User-Agent**,
silently, and across the entire source.
