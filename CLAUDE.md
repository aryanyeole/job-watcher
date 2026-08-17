# Job Watcher — context for Claude Code

Personal tool: polls company career-page APIs directly and pushes Telegram
alerts for new-grad SWE postings, to apply hours before LinkedIn surfaces them.

## Non-negotiable filtering rules

1. **Sponsorship is NEVER a filter.** The user is on OPT (start July 2026) and
   can work up to 3 years without H-1B sponsorship. Roles that say "we will not
   sponsor" are fully applyable and MUST come through. Sponsorship language is
   detected only to render an informational tag. Do not add a config option to
   filter on it.
2. **US citizenship / ITAR / security clearance IS the hard filter.** Those
   roles are genuinely closed on F-1/OPT. Controlled by
   `drop_citizenship_required_default`.

## Architecture

- `src/scrapers.py` — one function per ATS. All return `list[Job]` with a
  STABLE `id` (state-diffing depends on it; unstable ids = duplicate alerts).
- `src/filters.py` — title/location matching, citizenship detection, info tags.
- `src/health.py` — detects silently-broken scrapers. A board returning 0 jobs
  after previously returning many is the main failure mode; do not weaken this.
- `src/state.py` — set of seen job IDs.
- `src/notify.py` — Telegram formatting/delivery.
- `src/main.py` — orchestrator. `--dry-run`, `--seed`.

## Conventions

- Run `python -m pytest tests/ -q` after every change. 62 tests currently pass.
- Test with `--dry-run` before any live run.
- Never remove `time.sleep()` pacing or the retry backoff — Workday sits behind
  Akamai and will IP-block aggressive clients.
- Workday is a POST with a JSON body; tenant/shard/site are parsed from
  `careers_url` and cannot be guessed.
- Lever's `createdAt` is epoch MILLISECONDS.
- New scrapers need tests with mocked responses (see `tests/test_integration.py`).

## Common tasks

- Add a company → edit `companies.yaml`, run `--dry-run`, verify, commit.
- Add an ATS platform → new function in `scrapers.py`, register in `SCRAPERS`,
  add tests.
- Debug missing alerts → check `health.json` for that company's
  `last_job_count` and `consecutive_failures`.

## Scope constraint (important)

There is NO cross-company search API on Greenhouse, Lever, Ashby, or Workday.
Every endpoint is scoped to one company's board. The company list in
companies.yaml IS the query — breadth comes from having thousands of entries,
not from a better endpoint. Do not go looking for a global search API; it
doesn't exist.

Breadth is built by `src/discover.py`, which probes each ATS for candidate
slugs derived from company names in `data/companies_seed.txt`.

## Tiers

- `tier: hot` — ~20-50 real targets, polled every 30 min (business hours).
- `tier: bulk` — the long tail, swept twice daily. Default when omitted.

Never poll the bulk tier at hot frequency; thousands of boards x 48/day gets
the IP blocked, and a blocked scraper finds nothing.
