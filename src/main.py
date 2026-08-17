"""
Orchestrator: scrape -> filter -> diff -> notify.

TIERED POLLING
--------------
None of these ATS platforms offer cross-company search, so the company list IS
the query — which means you want THOUSANDS of companies, not a dozen. But you
cannot poll thousands of boards every 30 minutes: that's ~100k requests/day,
which is abusive and will get you blocked.

So companies carry a `tier`:

  tier: hot   — your real targets. Polled every 30 min during business hours.
                Keep this to ~20-50 companies.
  tier: bulk  — the long tail. Swept once or twice a day. Can be thousands.

A posting at a hot company reaches you in ~30-50 min. A posting anywhere else
reaches you within a day — still well ahead of a LinkedIn digest.

Flags:
  --tier hot|bulk|all   which tier to scrape (default: all)
  --dry-run             print instead of sending Telegram
  --seed                record current postings as seen WITHOUT alerting
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src import health as health_mod
from src import notify
from src.filters import evaluate
from src.scrapers import SCRAPERS, Job
from src.state import load_state, save_state

CONFIG_PATH = "companies.yaml"
STATE_PATH = "state.json"
HEALTH_PATH = "health.json"
HISTORY_PATH = "history.csv"

# Concurrency for the bulk sweep. 8 is polite — these are other people's
# servers, and Workday in particular will IP-block an aggressive client.
MAX_WORKERS = 8
DEFAULT_TIER = "bulk"


def append_history(jobs: list[Job]) -> None:
    """Permanent log — useful for spotting each company's posting cadence."""
    path = Path(HISTORY_PATH)
    is_new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if is_new:
            writer.writerow(["detected_at", "company", "title", "location", "flags", "url"])
        now = datetime.now(timezone.utc).isoformat()
        for j in jobs:
            writer.writerow([now, j.company, j.title, j.location, " ".join(j.flags), j.url])


def select_companies(companies: list[dict], tier: str) -> list[dict]:
    if tier == "all":
        return companies
    return [c for c in companies if c.get("tier", DEFAULT_TIER) == tier]


def scrape_one(company_cfg: dict) -> tuple[str, list[Job] | None, str | None]:
    """Returns (company_name, jobs, error). Never raises."""
    name = company_cfg.get("name", "<unnamed>")
    scraper = SCRAPERS.get(company_cfg.get("method"))

    if scraper is None:
        return name, None, f"unknown method '{company_cfg.get('method')}'"

    try:
        return name, scraper(company_cfg), None
    except Exception as exc:  # noqa: BLE001 — one bad board must not kill the run
        return name, None, str(exc)


def run(tier: str = "all", dry_run: bool = False, seed: bool = False) -> int:
    config = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8"))
    defaults = config.get("filters", {})
    all_companies = config.get("companies", []) or []
    companies = select_companies(all_companies, tier)

    if not companies:
        print(f"No companies in tier '{tier}'. Nothing to do.")
        return 0

    print(f"Tier '{tier}': {len(companies)} of {len(all_companies)} companies\n")

    seen = load_state(STATE_PATH)
    health = health_mod.load_health(HEALTH_PATH)

    new_jobs: list[Job] = []
    warnings: list[str] = []
    total_scraped = 0
    ok_count = 0
    fail_count = 0
    started = time.monotonic()

    by_name = {c.get("name"): c for c in companies}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(scrape_one, c) for c in companies]

        for future in as_completed(futures):
            name, jobs, error = future.result()

            if error is not None:
                fail_count += 1
                warnings += health_mod.record_failure(health, name, error)
                print(f"  [FAIL] {name}: {error[:120]}", file=sys.stderr)
                continue

            ok_count += 1
            total_scraped += len(jobs)
            warnings += health_mod.record_success(health, name, len(jobs))

            company_cfg = by_name.get(name, {})
            for job in jobs:
                if job.id in seen:
                    continue
                seen.add(job.id)

                should_alert, flags = evaluate(job, company_cfg, defaults)
                job.flags = flags
                if should_alert:
                    new_jobs.append(job)

    # Group by company for readability. Deliberately NOT sorted by sponsorship —
    # a non-sponsoring role is just as applyable on OPT and shouldn't be buried.
    new_jobs.sort(key=lambda j: (j.company, j.title))

    # --dry-run must not mutate anything. Persisting state during a preview
    # would silently mark every previewed posting as "seen", so a second
    # preview would show nothing and you'd lose the ability to re-check your
    # filters before going live.
    if not dry_run:
        save_state(STATE_PATH, seen)
        health_mod.save_health(HEALTH_PATH, health)

    elapsed = time.monotonic() - started

    if seed:
        save_state(STATE_PATH, seen)
        health_mod.save_health(HEALTH_PATH, health)
        print(
            f"\nSEED complete: {len(seen)} postings recorded as already-seen. "
            f"No alerts sent. Future runs report only genuinely new postings."
        )
        return 0

    if new_jobs and not dry_run:
        append_history(new_jobs)

    if dry_run:
        print("\n--- DRY RUN, no Telegram sent ---")
        print(notify.format_jobs(new_jobs) if new_jobs else "No new matching postings.")
        if warnings:
            print("\n" + notify.format_health_alert(warnings))
    else:
        if new_jobs:
            notify.send(notify.format_jobs(new_jobs))
        if warnings:
            notify.send(notify.format_health_alert(warnings))

    print(
        f"\n{ok_count} ok / {fail_count} failed | {total_scraped} postings scanned | "
        f"{len(new_jobs)} new & relevant | {len(warnings)} warning(s) | {elapsed:.1f}s"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Career-page job watcher")
    parser.add_argument(
        "--tier",
        choices=["hot", "bulk", "all"],
        default="all",
        help="hot = frequent targets, bulk = the long tail, all = everything",
    )
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending Telegram")
    parser.add_argument(
        "--seed",
        action="store_true",
        help="record current postings as seen without alerting (run once on setup)",
    )
    args = parser.parse_args()
    return run(tier=args.tier, dry_run=args.dry_run, seed=args.seed)


if __name__ == "__main__":
    sys.exit(main())
