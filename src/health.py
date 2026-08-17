"""
Health monitoring.

THIS IS THE MOST IMPORTANT FILE IN THE PROJECT and it is not the scrapers.

The failure mode that actually kills a tool like this isn't a crash — you'd
notice a crash. It's a scraper that quietly starts returning zero jobs
because a company changed their API shape or moved ATS. You keep getting no
alerts, you assume nothing is being posted, and you find out six weeks later
that you missed the exact req you built this for.

So: we track per-company job counts across runs and shout when a board that
used to return jobs suddenly returns none, or when a company errors on
several consecutive runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Alert after this many consecutive failures. 1 is too noisy (transient
# network blips happen); 3 risks a long silent window.
CONSECUTIVE_FAILURE_ALERT = 2


def load_health(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_health(path: str, health: dict) -> None:
    Path(path).write_text(json.dumps(health, indent=2, sort_keys=True))


def record_success(health: dict, company: str, job_count: int) -> list[str]:
    """
    Record a successful scrape. Returns a list of warnings to surface.
    """
    entry = health.setdefault(company, {})
    warnings = []

    previous_count = entry.get("last_job_count")
    if previous_count and previous_count > 0 and job_count == 0:
        warnings.append(
            f"{company}: returned 0 jobs but returned {previous_count} last run "
            f"— board may have moved or changed shape"
        )

    entry["last_job_count"] = job_count
    entry["last_success"] = datetime.now(timezone.utc).isoformat()
    entry["consecutive_failures"] = 0
    return warnings


def record_failure(health: dict, company: str, error: str) -> list[str]:
    """
    Record a failed scrape. Returns a list of warnings to surface.
    """
    entry = health.setdefault(company, {})
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    entry["last_error"] = error[:300]
    entry["last_failure"] = datetime.now(timezone.utc).isoformat()

    if entry["consecutive_failures"] >= CONSECUTIVE_FAILURE_ALERT:
        return [
            f"{company}: failed {entry['consecutive_failures']} runs in a row "
            f"— {error[:150]}"
        ]
    return []


def stale_companies(health: dict, hours: int = 72) -> list[str]:
    """Companies with no successful scrape in `hours`."""
    now = datetime.now(timezone.utc)
    stale = []
    for company, entry in health.items():
        last = entry.get("last_success")
        if not last:
            stale.append(company)
            continue
        try:
            delta = now - datetime.fromisoformat(last)
            if delta.total_seconds() > hours * 3600:
                stale.append(company)
        except ValueError:
            stale.append(company)
    return stale
