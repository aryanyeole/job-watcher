# Job Watcher

Polls company career-page APIs directly and pushes a Telegram alert the moment
a matching role is published — hours ahead of LinkedIn, Simplify, or Jobright.

**Status:** 84 tests passing. Filtering, state-diffing, health monitoring, and
the full pipeline are verified end-to-end against mocked API responses.

---

## Why this beats a LinkedIn job alert

A posting travels through this chain:

```
T+0       Recruiter clicks "publish" in Greenhouse / Workday / etc.
T+0       ← ATS public JSON API serves it IMMEDIATELY  ◄── WE POLL HERE
T+0       Company careers page shows it (the page calls that same API)
T+2-12h   LinkedIn / Indeed / Simplify crawlers discover it
T+6-24h   Aggregator ingests, dedupes, categorises
T+12-36h  LinkedIn alert email — frequently a DAILY DIGEST, not instant
```

The key point is line 2. **A company's careers page is not a website we're
scraping — it's a JavaScript app calling a public JSON API.** This tool calls
that exact same API. There's no crawl delay because there's no crawl; we're a
peer of the careers page, not a downstream consumer of an aggregator.

LinkedIn is structurally 12–36 hours behind and can't fix it — they must
discover, ingest, dedupe, and batch across millions of postings. You're polling
a few dozen endpoints.

Three advantages beyond raw speed:

- **No ghost jobs.** The ATS API only returns currently-published reqs.
  Aggregators are full of postings closed weeks ago.
- **Filtering you control.** LinkedIn's "New Grad" filter misses reqs titled
  *"Software Engineer, Early Career, Campus"*. Here you own the keyword logic.
- **Description-level signal.** We read the full posting body, so citizenship
  and clearance requirements are detected before you waste an application.

### What this does and doesn't do

It buys you a **latency edge measured in hours.** That's real and it matters
for high-volume reqs reviewed on a rolling basis — being applicant #40 instead
of #3,000 is a genuine advantage. It does not fix a resume, pass an OA, or
replace a referral. Speed amplifies a strong application; it doesn't create
one. Keep applying while you build this, not after.

---

## Scope: there is no cross-company search

**This is the most important thing to understand about the design.**

None of these ATS platforms offer a search-everything endpoint. Greenhouse's
own documentation states that filtering and searching are not possible on the
Job Board API — you fetch `/v1/boards/{board_token}/jobs` and get that one
company's postings. Lever, Ashby, and Workday are the same. There is also no
public directory of which companies use which ATS, and it changes weekly.

So there is no way to ask "show me every new-grad SWE role on Greenhouse."
**The company list IS the query.** A 16-company list is a 16-company search.

The fix isn't a different API — it's a much bigger list, built automatically:

```bash
python -m src.discover --input data/companies_seed.txt --merge companies.yaml
```

That probes Greenhouse, Lever, Ashby, and SmartRecruiters for every name in the
seed file, auto-detects which platform each uses, validates the slug, and merges
the results in. The shipped seed file has ~350 companies; add more names any
time and re-run. Re-running never clobbers your manual `tier` or keyword
overrides.

Companies that run their own careers infrastructure — Google, Amazon, Meta,
Apple, Microsoft, Netflix — will come back as "no public ATS board found". Those
need `custom_json` entries you build via devtools (see below).

### Tiered polling

You cannot poll thousands of boards every 30 minutes. 2,000 boards x 48
polls/day is ~96,000 requests — abusive, and it will get your IP blocked. So
companies carry a tier:

| Tier | Size | Frequency | Latency to alert |
|---|---|---|---|
| `hot` | ~20-50 | every 30 min, business hours | ~30-50 min |
| `bulk` | thousands | twice daily | up to ~12h |

`bulk` is the default when `tier` is omitted, so discovered companies land
there automatically. Promote your real targets to `tier: hot` by hand.

Even the bulk tier beats a LinkedIn daily digest, and the hot tier is where
your genuine first-mover advantage lives.

---

## Filtering policy

**Only ONE thing causes a posting to be dropped on description content: a US
citizenship, US-Person/ITAR, or security-clearance requirement.** Those roles
are genuinely closed on F-1/OPT, so they're noise.

**Sponsorship is never a filter.** A company that won't sponsor H-1B can still
hire you on OPT for up to three years — those are real, applyable jobs and they
always come through. Sponsorship language is detected and shown as an
informational tag (`ℹ️ no H-1B sponsorship (OPT still fine)`) so you know what
you're walking into. There's deliberately no config option to filter on it.

Postings are also dropped on title keywords and location, both of which you
control in `companies.yaml`.

---

## Architecture

```
companies.yaml ──► scrapers.py ──► filters.py ──► main.py ──► notify.py ──► Telegram
                   (ATS APIs)      (relevance)     (diff)      (format)
                                                     │
                                          state.json │ health.json │ history.csv
                                          (seen IDs) │ (breakage)  │ (audit log)
```

| File | Role |
|---|---|
| `src/scrapers.py` | One function per ATS platform. Retries with exponential backoff. |
| `src/filters.py` | Title/location matching + citizenship detection + info tags. |
| `src/health.py` | **Detects silently-broken scrapers.** See below. |
| `src/state.py` | The set of job IDs already seen — prevents duplicate alerts. |
| `src/notify.py` | Telegram formatting + delivery, with message chunking. |
| `src/main.py` | Orchestrates everything. `--tier`, `--dry-run`, `--seed` flags. |
| `src/discover.py` | Probes ATS platforms to auto-build a large company list. |

### Why `health.py` is the most important file

The failure mode that kills a tool like this isn't a crash — you'd notice a
crash. It's a scraper that **quietly starts returning zero jobs** because a
company changed their API shape or migrated ATS. You keep getting no alerts,
assume nothing is being posted, and find out six weeks later that you missed
the exact req you built this for.

So the tool tracks per-company job counts across runs and alerts you when:
- a board that returned jobs last run returns zero now
- a company fails two consecutive runs

Take those warnings seriously. A silent scraper is worse than no scraper.

---

## Supported platforms

| Method | Endpoint | Notes |
|---|---|---|
| `greenhouse` | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` | GET, no auth. Descriptions inline via `content=true`. |
| `lever` | `api.lever.co/v0/postings/{slug}?mode=json` | GET, no auth. `createdAt` is epoch **milliseconds**. |
| `workday` | `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` | **POST** with JSON body. See gotchas below. |
| `ashby` | `api.ashbyhq.com/posting-api/job-board/{slug}` | GET, no auth. |
| `smartrecruiters` | `api.smartrecruiters.com/v1/companies/{slug}/postings` | GET, paginated. |
| `custom_json` | company-specific | For Google/Amazon/Meta/Apple/Microsoft/Netflix. |
| `playwright` | rendered DOM | Last resort only. |

### Workday gotchas (these cost people hours)

1. **It's a POST, not a GET.** A GET returns the HTML shell of the careers
   page — which is why people conclude "there's no API" and reach for Selenium.
2. **You cannot guess the URL.** `{tenant}`, the `wd{N}` shard, and `{site}`
   are all company-specific with no derivable rule. NVIDIA is
   `wd5/NVIDIAExternalCareerSite`; Salesforce `wd12/External_Career_Site`;
   Adobe `wd5/external_experienced`. Read them off the real careers URL — the
   scraper parses them for you from `careers_url`.
3. **Akamai bot management.** Send a browser UA and a matching `Referer`, and
   pace yourself. Aggressive retries get your IP blocked, which silently kills
   the tool.

---

## Setup

### 1. Telegram bot (~2 min)

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, follow prompts.
   You get a **bot token** → `TELEGRAM_BOT_TOKEN`.
2. Message your new bot anything so it can see your chat.
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` — find
   `"chat":{"id": ...}` → `TELEGRAM_CHAT_ID`.

### 2. Test locally first

```bash
pip install -r requirements.txt
python -m pytest tests/ -q                 # should be 84 passed
python -m src.main --tier hot --dry-run    # prints results, sends nothing
```

`--dry-run` needs no Telegram credentials. Run it and read the output before
you trust anything.

### 3. Build your company list

```bash
python -m src.discover --input data/companies_seed.txt --merge companies.yaml
```

Takes a few minutes for ~350 names. Add more names to the seed file and re-run
whenever you want more coverage — this is the single highest-leverage thing you
can do for this tool, because the company list is the search.

Then promote your real targets to the hot tier by adding `tier: hot` to their
entries in `companies.yaml`.

### 4. Seed the state file

```bash
python -m src.main --tier all --seed
```

This records every currently-open posting as already-seen **without alerting**.
Skip this and your first real run will fire hundreds of notifications for jobs
that have been open for months.

### 5. Push to GitHub and add secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Commit `state.json` so the seeded baseline goes up with the code.

### 6. Trigger one manual run

Actions tab → Job Watcher → Run workflow. Confirm it completes and that a
Telegram message arrives (or that it cleanly reports zero new postings).

---

## Adding companies

### Already-supported ATS

Just add a `companies.yaml` entry. To find a slug, try
`https://boards.greenhouse.io/<guess>` or `https://jobs.lever.co/<guess>` with
the company's obvious lowercase name. If a real jobs page loads, that's it.

For Workday, visit the company's careers page and copy the full
`*.myworkdayjobs.com` URL into `careers_url` — parsing is automatic.

### Custom career sites (Google, Amazon, Meta, Apple, Microsoft, Netflix)

These run their own infrastructure. Find the endpoint:

1. Open the careers page in Chrome.
2. DevTools (F12) → **Network** tab → filter to **Fetch/XHR**.
3. Reload. Apply a filter on the page (e.g. search "new grad") to trigger the
   request if it lazy-loads.
4. Find the request returning JSON with job titles. Click → **Response** tab
   to inspect the shape.
5. Note the URL, whether it's GET or POST, the path to the jobs array, and the
   field names.
6. Write a `custom_json` entry (template is in `companies.yaml`).
7. **Test with `--dry-run` before adding the next one.** One at a time.

---

## Tuning

| Want to... | Do this |
|---|---|
| Catch more roles | Add title variants to `keywords_any_default` |
| Cut noise | Add to `keywords_none_default` |
| Widen geography | Add to `locations_default`, or empty the list for everywhere |
| See clearance roles too | `drop_citizenship_required_default: false` |
| Poll more often | Edit `cron:` in `.github/workflows/scrape.yml` |
| Cover more companies | Add names to `data/companies_seed.txt`, re-run discovery |
| Watch a company closely | Add `tier: hot` to its entry |
| Override per company | Put `keywords_any` / `locations` on that company's entry |

`history.csv` logs every posting ever detected — useful for spotting each
company's posting cadence so you know when to expect the next cycle.

---

## Going faster than GitHub Actions

GitHub's scheduled workflows are best-effort. Under load, runs are commonly
delayed 5–20 minutes and occasionally skipped. With the default 30-minute
interval your real latency is roughly 30–50 minutes — still many hours ahead of
LinkedIn.

If you want tighter, move to a small always-on box (a $5/mo VPS, or a Raspberry
Pi) with a plain crontab:

```
*/10 * * * * cd /path/to/job-scraper && /usr/bin/python3 -m src.main >> run.log 2>&1
```

The code is identical — only the scheduler changes. Swap `state.json` for
SQLite or Redis if you outgrow the flat file.

---

## Etiquette

- These are public endpoints companies publish so candidates can find jobs.
  Polling them a few dozen times a day is lighter than a human refreshing.
- Keep the built-in backoff. Don't remove the `time.sleep()` calls between
  pages.
- If a source errors consistently, back off and investigate — don't retry
  harder. Getting IP-blocked defeats the entire purpose.
- Respect `robots.txt` if you add a `playwright` target.
