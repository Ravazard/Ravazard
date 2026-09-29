# Job Hunter

Finds job openings that match your skills, scores each one, and fills out
and submits the application for you in a real browser.

```
search  ->  score  ->  you approve  ->  fill form (dry run)  ->  --submit
```

- **Sources:** public job-board APIs from Greenhouse, Lever and Ashby (used by
  thousands of companies' careers pages), plus Remotive and RemoteOK for remote roles.
- **Matching:** title, skills, city, years of experience and salary. Each job
  gets a 0–100 score and a list of reasons. Claude can optionally re-rank the
  matches (`--llm`).
- **Applying:** Playwright opens the form, answers each question from your
  profile, uploads your resume, and adds a cover letter.
- **Safe by default:** it runs in dry-run mode unless you pass `--submit`. It
  stops at CAPTCHAs, never makes up answers to required questions, and never
  applies to the same job twice. Every form gets a screenshot.

## Setup

```bash
cd job-hunter
pip install -r requirements.txt
playwright install chromium

cp profile.dba.example.yaml profile.yaml   # or profile.example.yaml for a blank template
# edit profile.yaml, then put your resume next to it as resume.pdf
```

## Example: open-source DBA roles in Bengaluru and Chennai, 2 yrs 2 months of experience

`profile.dba.example.yaml` is already set up for this search:

```yaml
personal:
  years_experience: 2.17          # 2 years 2 months
search:
  titles: [Database Administrator, Database Engineer, MySQL DBA, MariaDB DBA, Open Source DBA, PostgreSQL DBA]
  locations: [Bengaluru, Chennai] # "Bangalore" and "Madras" also match
  require_core_skill: true        # posting must mention MySQL or MariaDB
  exclude_title_keywords: [principal, staff, architect, lead, manager, oracle, sql server, db2, ...]
skills:
  core:  [MySQL, MariaDB]                        # weight 2
  other: [PostgreSQL, Linux, Shell Scripting, ...] # weight 1
  basic: [MongoDB, ArangoDB]                     # weight 0.5
```

- A "Database Administrator" posting that only mentions Oracle, SQL Server
  or MongoDB is rejected.
- A MySQL- or MariaDB-first role ranks above a Postgres-first role that also
  mentions MySQL.
- Roles asking for much more experience than you have (for example 5–8
  years) drop below the threshold. Senior titles are excluded outright.

```bash
python -m jobhunter search              # fetch and score; prints the matches
python -m jobhunter show greenhouse:acme/101
python -m jobhunter approve --top 10    # or: approve KEY KEY ...
python -m jobhunter apply               # DRY RUN: fills forms and saves screenshots to ./screenshots
python -m jobhunter apply --submit      # actually submits them
python -m jobhunter list --status needs_manual   # ones that need you (captcha, custom questions)
python -m jobhunter stats
```

Once you trust it, you can set `auto_approve: true` and `dry_run: false` and
run it on a schedule:

```cron
0 9 * * *  cd ~/job-hunter && python -m jobhunter run --submit >> run.log 2>&1
```

`max_per_run` caps how many applications go out per run.

## Adding companies

List company slugs under `sources:` in `profile.yaml`. Open a company's
careers page and click a job. If the URL looks like one of these, add the slug:

| URL | add under |
|---|---|
| `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `greenhouse` |
| `jobs.lever.co/<slug>` | `lever` |
| `jobs.ashbyhq.com/<slug>` | `ashby` |

A wrong slug is logged as `FAILED` and skipped. It doesn't stop the run.

## What it deliberately does not do

- **It doesn't scrape Naukri, LinkedIn, Indeed, Foundit or Glassdoor.** Their
  terms forbid bots, and automated applying there gets accounts banned. Many
  Indian DBA openings are posted only on those sites, so use their own
  job alerts and apply to those by hand. This tool covers companies that host
  their own careers page on Greenhouse, Lever or Ashby.
- **It doesn't solve CAPTCHAs.** Those jobs are marked `needs_manual`.
- **It doesn't guess.** A required question it can't answer from your profile
  or the `answers:` section stops the application. The job is marked
  `needs_manual` and the question is listed, so you can add an answer to
  `answers:` and re-approve it.

## Using Claude (optional)

Set `ANTHROPIC_API_KEY`, then:

- `search --llm` asks Claude to re-rank new matches and list missing skills.
- `use_llm_for_cover_letter: true` writes a tailored cover letter for each job.
- `use_llm_for_questions: true` drafts answers to screening questions, using
  only facts in your profile. If the profile doesn't cover a question, the job
  goes to `needs_manual`.

These features use `claude-opus-5-5` with Anthropic's server-side refusal
fallback turned on.

## Development

```bash
pytest -q        # set JOBHUNTER_CHROMIUM=/path/to/chrome to use a specific browser
```
