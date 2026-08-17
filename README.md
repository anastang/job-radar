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

| Source | Freshness field | Description text |
|---|---|---|
| Greenhouse | `first_published` — true publish time | yes (`?content=true`) |
| Ashby | `publishedAt` | yes, plus compensation |
| Lever | `createdAt` (epoch ms) | yes, plus salary range |
| SmartRecruiters | `releasedDate` | no — but gives structured `experienceLevel` |
| SimplifyJobs new-grad feed | `date_posted` | no — breadth backstop |

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

Survivors are scored out of 100: role family (36), resume skill overlap (32), location
(22), early-career signals (6), freshness (4). Postings without a description are
normalized against the points actually available rather than being penalized for text
they never had — otherwise a real "Data Engineering New Grad" ranks below a generic
role that merely had more text to keyword-match.

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
| `config/companies.yaml` | generated — company slugs by ATS and tier |
| `config/profile.yaml` | generated — resume-derived skill terms and weights |

Regenerate the company list periodically; boards migrate between ATS vendors:

```bash
python scripts/validate_companies.py
```

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
