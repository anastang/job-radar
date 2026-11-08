# Job Radar

[![Tests](https://github.com/anastang/job-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/anastang/job-radar/actions/workflows/tests.yml)

Job Radar watches about 600 company job boards for early-career **data engineering,
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
| Amazon | `posted_date` | yes | yes, including qualifications |
| Oracle HCM | `PostedDate` | yes | short description only |
| SmartRecruiters | `releasedDate` | yes | no, but gives `experienceLevel` |
| Rippling | `createdOn`, on the detail endpoint only | yes | yes, plus pay ranges |
| Workday | `postedOn`, prose like "Posted 13 Days Ago" | coarse | no |
| SimplifyJobs new-grad feed | `date_posted` | **no, it is an index date** | no |
| jobright new-grad feed | table column | **no, it is an index date** | no |

That covers nine ATS providers plus two community feeds, roughly 1,190 boards,
including about 350 YC startups and 188 companies found through aggregator sitemaps.

Amazon and Oracle HCM are queried rather than enumerated. Both boards are far too
large to walk, so each is swept with a handful of role-specific searches and the
results deduplicated, the same approach Workday uses. Oracle boards are addressed by
host and site number together, encoded as `host/siteNumber`, because one adapter
serves every Oracle customer.

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

### The same role, filed many times over

The jobright feed carries reposts. One employer had 39 copies of a single remote
"Data Analyst" listing in the Data-Analysis feed and a second had 19, together 13% of
that whole feed. Every copy carries its own listing id, so dedupe by id keeps all of
them, and all 58 were reaching Discord as separate alerts.

The adapter collapses rows on company, title and location instead. Location stays in
the key because a real multi-site posting is a different job to apply to at each site:
Gotion lists the same analyst role in Fremont and in Manteno, and both are worth
seeing. The surviving row is chosen by lowest listing id rather than by feed position,
so a reordered feed does not hand the same role a new dedupe key and alert it twice.
See `collapse_repeats` in `sources/jobright.py`.

### Rippling needs two requests per posting

Rippling is the only provider that splits what we need across two endpoints. Its
board listing is cheap but thin, carrying a uuid, title, department and one location
string, with **no posting date and no description at all**. Both live on a
per-posting detail endpoint.

That shape cannot be ignored. Without a date every Rippling posting would bypass the
staleness gate and score as unknown-age forever; without a description the
generic-engineering gate could never admit one.

What makes fetching details affordable is filtering on the title first. On Rippling's
own board, 665 live postings reduce to 19 once senior, intern and plainly irrelevant
titles are dropped, which is 2.9%. One list request plus about twenty small detail
requests per board buys real dates, real descriptions and published pay ranges.

The pre-filter re-uses the same title predicates the real gate applies later, so it
can only discard postings `evaluate` would have rejected anyway. Two further quirks:
the readable employment type sits in `id` rather than `label`, and pay is published
per period, so monthly bands are dropped rather than shown as a salary a tenth of the
real figure.

### Where the company list comes from

ATS APIs have no global search, so the tool can only poll companies it already knows
by name. Curating that list by hand is the wrong job for a human, because most
applications go to companies you have never heard of and hearing of them is the hard
part.

Aggregator sites have already solved exactly that, and three of them put the company
and the job title **into the URL** and publish every URL in a sitemap:

    https://hiringtempo.com/company/stripe/jobs/account-executive-platforms-grower-060e3115
    https://himalayas.app/companies/onepilot/jobs/data-engineer
    https://startup.jobs/forward-deployed-software-engineer-palantir-23813

So a handful of sitemap fetches yield tens of thousands of company-and-title pairs
with no page scraping, and the titles let us keep only companies hiring for something
relevant right now. `scripts/discover_from_boards.py` does that, then resolves each
company name to an ATS board.

The yield is far better than the other generators: **92%**, against 34% for YC and
35-44% for VC portfolios. The reason is that these companies are known to be hiring a
relevant role at this moment, and their slugs are clean. One run added 188 boards and
took the pollable total from 1,000 to 1,188. Those new boards produced 152 alerts on
the next sweep, including Cerebras, CoreWeave, Chainguard, Airwallex, Abnormal
Security and CircleCI, none of which were reachable before.

Neither site's API is touched: hiringtempo disallows `/api/` in robots.txt and
startup.jobs disallows its apply paths, and nothing here requests either. The job
pages themselves are never fetched either, because the ATS the company already runs
is a better source for the posting than an aggregator's copy, and polling it directly
is what makes this tool early.

The pool is much larger than one run should resolve politely, so each run takes the
next batch and the committed yaml makes already-resolved companies skippable next
time. Measured across the three sources, roughly **6,000 companies** are identified
as currently hiring a relevant role and not yet polled: ~205 from hiringtempo, ~2,060
from himalayas and ~4,020 from startup.jobs, with overlap between them.

`.github/workflows/discover.yml` runs this twice a week and commits what it finds, so
the company list grows without anyone going looking for employers to add. It warns
when a run resolves nothing, because that usually means an aggregator changed its URL
shape, which otherwise looks exactly like a successful run.

startup.jobs is supported but off by default. It is larger, at 272,407 job URLs
behind 4,320 relevant-hiring companies, but its company slugs carry legal-entity
prefixes from a bulk import, so the same employer appears as `0020-paypal-inc` and
`100-salesforce-inc`. Even after cleaning those, resolving the full pool would take
roughly 72,000 probe requests, which is more traffic than one job search should put on
other people's endpoints. Run it deliberately with `--source startupjobs --limit`.

Its post URLs also need the company list it publishes separately: nothing in the URL
marks where the title ends and the company begins, both halves contain hyphens, and
guessing the last token produced companies called "1", "10" and "091614".

### Searching the whole of a board host (built, off by default)

Direct polling only ever sees companies on the list. A search engine has crawled all
of `jobs.ashbyhq.com`, so `"New Grad" site:ashbyhq.com` reaches employers no
portfolio page or aggregator ever named. `config/searches.yaml` holds the query list,
crossed as `"<term>" site:<site>`, and `search_every_minutes` in `config.yaml` sets
the cadence.

It ships disabled, because this is the only part of the tool that costs money. There
is no free programmatic search left: Google's Custom Search JSON API is closed to new
customers as of 2026, its site-restricted variant stopped serving traffic in January
2025, and Brave's free allowance is credit-based behind a card. At 58 queries a run
that is roughly $35/month at six hours and $209/month hourly. Set
`search_every_minutes` and `BRAVE_SEARCH_API_KEY` to turn it on, and run
`scripts/probe_search.py` first to confirm the response parsing against a live key.

The free route to the same outcome is the sitemap discovery below, which reaches more
companies than these queries would.

The design choice that matters is what happens to a hit. A search result is a weak
object: a title, a URL and a snippet, with no posting date, no years-of-experience
line and no stack list, so it cannot be gated or scored properly. So results are not
treated as postings. The company slug is parsed out of the URL and that company's
real ATS board is fetched through the normal adapter, which returns the employer's
own date and full description. Three things follow, and they are the point:

* Postings arrive with everything the gate and the four-track scorer need.
* The dedupe key is identical to direct polling's, so a role found both ways alerts
  once rather than twice.
* The company is remembered in the state file and polled every few minutes from then
  on. The search only has to find an employer once.

That last point is worth understanding before paying for a fast cadence. The search
lag applies to a company's **first** posting and never to its later ones, so going
from daily to hourly makes a brand-new employer's debut arrive sooner and changes
nothing for the 1,188 boards already polled.

Two practical notes. Scraping a search engine's results page is against the terms of
every major one and gets CAPTCHA'd, so this uses an official API; Google's Custom
Search JSON API is closed to new customers as of 2026 and its site-restricted variant
stopped serving traffic in January 2025, which leaves Brave's API as the usable
option. And a missing key raises rather than returning an empty list, because a
silent zero from this source reads exactly like a quiet day.

LinkedIn is handled differently on purpose. Its robots.txt is
`User-agent: * / Disallow: /`, so nothing here ever fetches a LinkedIn page. Those
hits are used as search metadata only, with no date and no description, and they
score through the same path the community feeds use. The alert links out for you to
open yourself.

### What a keyword search alone would miss

Worth recording, because it is why the search source supplements the boards rather
than replacing them. Searching only the early-career phrases looks appealing and
performs terribly: measured against 727 alerts carrying a description, `"New Grad"`
appears in 2.5%, `"Early Career"` in 0.8%, `"Entry Level"` in 0.1%, and **any** of
them in 3.3%. The other 96.7% are titled "Data Engineer I" or "Software Engineer II"
with "1-2 years" in the body, and are found by parsing the years requirement out of
the description rather than by matching a phrase.

That is why `searches.yaml` crosses the role titles as well as the markers, and why
the gate still runs on the full posting afterwards.

### Why contact emails are not scraped

Emailing recruiters and hiring leads directly has been the highest-response channel in
this search, so the alert carries two LinkedIn people searches: one for recruiters at
the company, one for the functional lead that matches the winning track.

It does not extract email addresses from postings, and that is a measured decision.
Across 4,007 described postings from Greenhouse, Ashby and Lever, 949 email addresses
appear in the body and **97.4% of them are accommodation or accessibility addresses**,
the line employers are legally required to publish so candidates can request
interview adjustments. Four occurrences were a generic careers inbox. None were a
named person. An "extract the contact email" feature would therefore surface an ADA
compliance queue and label it a hiring contact, which is both useless and the wrong
thing to send a cold email to.

Third-party contact databases would supply real addresses, but they hold personal
data gathered without the subject's involvement, and using one here would mean this
tool building a file on people who never published an address at all. A search the
user runs as themselves lands on the same people, with their current titles, and
stores nothing. `tests/test_outreach.py` has a regression guard that fails if an
email regex ever appears in that module.

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

## Four resumes, four tracks

There are four tailored resumes: Data, AI, FDE and SWE. The tool models them as four
separate targets, scores every posting against all four, and keeps the best result.
The winning track is shown on the alert as the resume to send.

That is the point of the design rather than a side effect. Once a role is worth
applying to, the first decision is which resume to attach, and a single blended
profile cannot answer it. It also scores worse: averaging the four would judge a
React-and-pytest posting on dbt terms it never mentions, and a dbt posting on React
terms it never mentions, rating both mediocre instead of recognising each as a strong
fit for one track.

Weights are per track, because the same technology is worth different amounts
depending on which resume is going out. Airflow is a headline on the Data resume and
a supporting detail on the AI one, so it is weighted 3.0 there and 1.75 here.

Two rules decide the track:

* **Skill overlap picks the winner.** Each track scores as a fraction of its own
  target, so the tracks are comparable despite holding different numbers of terms.
* **A close race is broken by the job title.** When the family's natural track is
  within 0.10 of the leader, the title wins, because the title is what a reviewer
  screens the resume against. A decisive gap is never overridden: a posting titled
  "Data Engineer" whose body is entirely RAG and evals is still answered with the AI
  resume.

On a live sweep of 1,188 boards the four tracks split the alerts 29% AI, 27% Data,
25% FDE and 19% SWE, which is roughly the balance the four resumes imply.

Groups named `foundational`, `platform`, `infra`, `storage` and `adjacent` are
excluded from the "core" vocabulary that decides whether a generic engineering title
is relevant at all. Python, Docker, AWS and PostgreSQL appear in nearly every
engineering posting, so counting them would make all four tracks look alike.

## How filtering works

A posting must clear every gate before it is scored:

1. **Role family.** Data engineer, analytics engineer, data analyst, AI engineer and
   forward-deployed roles all carry full weight, with data scientist, BI, ML engineer
   and research engineer below them. Generic engineering titles, including "Member of
   Technical Staff", are admitted by any one of three independent tests: the body
   carries at least three distinct data or AI signals, it names at least two
   technologies from the Data, AI or FDE tracks, or it names at least three from the
   SWE track.

   The third test arrived with the tailored SWE resume, and it is deliberately
   stricter than the others because SWE vocabulary is the most likely to appear
   incidentally. Keeping the SWE track *out* of the second test matters just as much:
   when it was included, "React" plus "pytest" counted as two adjacency hits, 354
   postings came in through the loose route, and the stricter route never fired once.
   Separated, the SWE route admits 32 postings that nothing else catches.

   Forward-deployed roles were promoted from a 0.85 weight and a pattern matching
   only the literal phrase. That pattern missed the titles these roles actually
   carry: Databricks posts "Solutions Engineer", Pallet posts "Associate Deployment
   Strategist", Materialize posts "Forward Deployed Engineer", and all three now
   surface in the low 90s.

   Generic engineering titles in a different discipline are rejected outright rather
   than scored down. An embedded firmware or Unreal rendering role is not a near miss
   on this resume, it is a different job, and 402 such titles were filtered on a
   single sweep.
2. **Seniority.** Senior, staff, principal, lead, and manager titles are rejected, as
   is level III and above. The description is then parsed for a years-of-experience
   requirement, because the strongest matches often carry no seniority marker in the
   title. Anything above `max_years_experience` (default 3) is rejected.

   One exception is worth knowing about. "Member of Technical Staff" is the standard
   individual-contributor title at OpenAI, Anthropic, Mistral and most frontier labs,
   and it spans every level including new grad. Matching the bare word "staff"
   rejected all of them, which quietly removed that entire tier of companies. The
   pattern now carries a negative lookbehind, while "Staff Data Engineer" and
   "Senior Member of Technical Staff" are still rejected.
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
| `config/companies_vc.yaml` | generated, venture portfolio companies |
| `config/companies_watchlist.yaml` | **hand-maintained**, never touched by a generator |
| `config/companies_feed.yaml` | generated, Workday, Workable, Oracle HCM and Amazon |
| `config/companies_boards.yaml` | generated, companies found hiring on aggregator sitemaps |
| `config/profile.yaml` | **hand-maintained**, the four resume tracks and their weights |

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
python scripts/discover_vc.py
```

```bash
python scripts/discover_from_feed.py
```

### Adding a company you heard about somewhere

Broad sweeps will never cover a company you happen to hear about from a friend, a
news story, or a post. That gap is real and it was measured: of eight companies found
by hand and heard back from, four were reachable on boards this tool already polls and
were missed anyway, purely because nothing had named them.

```bash
python scripts/add_company.py "Charta Health" "Distyl AI"
```

The name is turned into candidate slugs, probed against every provider, and whatever
resolves is appended to `config/companies_watchlist.yaml`. That file is hand-owned, so
no generator overwrites it, and its entries are tier1 because a company added
deliberately deserves more attention than one that arrived from a sweep.

Not every company resolves. Some run their own portal with no standard ATS behind it,
in which case the script says so rather than guessing.

### Why there are several generators

`validate_companies.py` probes a hand-maintained list of established companies.

`discover_yc.py` walks the public YC directory, keeps companies that are active,
hiring, and in a major North American hub, and guesses each ATS slug from the company
name and website domain. About a third resolve, helped by the directory's `isHiring`
flag and by YC names mapping to slugs predictably.

`discover_vc.py` does the same for venture portfolio companies, walking a16z, Index
Ventures, Founders Fund and General Catalyst. Expect a lower yield than YC, because
portfolio pages carry no hiring signal, so many probes land on companies that are not
recruiting.

Each portfolio needed its own extraction rule, verified against the live page. Some
firms label entries with their own name appended ("Affirm - Founders Fund"), which has
to be stripped or the slug resolves to nothing, and nav labels sit in the same markup
as the company names. Accel, Techstars and Creative Destruction Lab are deliberately
absent: their listings render client-side and the HTML yields zero names. Sequoia
yields 21, all household brands already covered. Adding any of them would contribute
nothing while appearing to work, so `discover_vc.py` warns when a source returns fewer
than 25 names rather than failing quietly.

`discover_from_feed.py` harvests Workday, Workable, and Oracle HCM boards from real
posting URLs in the community feed. Those boards are addressed by host, tenant, and
site together (`ngc.wd1` / `ngc` / `Northrop_Grumman_External_Site` for Workday,
`host/siteNumber` for Oracle), which makes them impossible to guess. Blocked employers are dropped during discovery rather than at filter time, so no
poll is spent on a board whose every posting would be rejected.

`config/profile.yaml` is hand-maintained, because the four tracks carry per-track
weights that cannot be derived from one PDF. When a resume changes, edit the matching
track.

`build_profile.py` still exists to extract candidate terms from a resume, but it
emits the older single-profile format and now refuses to write to
`config/profile.yaml`, because doing so would collapse four targets into one and the
alerts would stop naming which resume to send. Write it somewhere scratch and fold
anything useful into the right track by hand:

```bash
python scripts/build_profile.py --resume "path/to/resume.pdf" --out /tmp/terms.yaml
```

## Tests

```bash
python -m pytest
```

Add `-m "not live"` to skip the two tests that hit real endpoints, which is what CI
does. Keep them for local runs. One guards a requirement that is easy to reintroduce:
**Ashby returns 404 for every request that arrives without a browser User-Agent**,
silently, and across the entire source.
