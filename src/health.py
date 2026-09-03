"""
Health monitoring.

ALERT THROTTLING — the second-order failure:
Warning on EVERY run for the same broken board is just as bad as not warning
at all. Twenty-eight identical "Spring Health failed" messages bury the one
real job alert underneath them, and you learn to swipe the warnings away
without reading. So each problem alerts once when it appears, then goes
quiet, then reminds you roughly once a day until it's fixed or removed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

CONSECUTIVE_FAILURE_ALERT = 2
REMIND_AFTER_RUNS = 45      # ~45 runs/day = roughly one reminder per day
LIKELY_DEAD = 100


def load_health(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_health(path: str, health: dict) -> None:
    Path(path).write_text(json.dumps(health, indent=2, sort_keys=True), encoding="utf-8")


def record_success(health: dict, company: str, job_count: int) -> list[str]:
    entry = health.setdefault(company, {})
    warnings: list[str] = []

    previous_count = entry.get("last_job_count")
    if previous_count and previous_count > 0 and job_count == 0:
        if _should_alert(entry, "zero_jobs"):
            warnings.append(
                f"{company}: returned 0 jobs but returned {previous_count} last run "
                f"— board may have moved or changed shape"
            )
    else:
        entry.pop("zero_jobs_alerted_at_run", None)

    entry["last_job_count"] = job_count
    entry["last_success"] = datetime.now(timezone.utc).isoformat()
    entry["consecutive_failures"] = 0
    entry.pop("failure_alerted_at_run", None)
    entry["runs"] = entry.get("runs", 0) + 1
    return warnings


def record_failure(health: dict, company: str, error: str) -> list[str]:
    entry = health.setdefault(company, {})
    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
    entry["last_error"] = error[:300]
    entry["last_failure"] = datetime.now(timezone.utc).isoformat()
    entry["runs"] = entry.get("runs", 0) + 1

    failures = entry["consecutive_failures"]
    if failures < CONSECUTIVE_FAILURE_ALERT:
        return []
    if not _should_alert(entry, "failure"):
        return []
    if failures >= LIKELY_DEAD:
        return [f"{company}: DEAD after {failures} failures — remove it from companies.yaml"]
    return [f"{company}: failed {failures} runs in a row — {_short_error(entry['last_error'])}"]


def _should_alert(entry: dict, kind: str) -> bool:
    key = f"{kind}_alerted_at_run"
    runs = entry.get("runs", 0)
    last_alerted = entry.get(key)
    if last_alerted is None or runs - last_alerted >= REMIND_AFTER_RUNS:
        entry[key] = runs
        return True
    return False


def _short_error(error: str) -> str:
    """Strip the noisy URL tail so alerts stay readable on a phone."""
    if "404" in error:
        return "404 Not Found (slug changed or board removed)"
    if "403" in error:
        return "403 Forbidden (blocked or auth required)"
    if "timeout" in error.lower() or "timed out" in error.lower():
        return "timeout"
    return error.split(" for url")[0][:120]


def dead_companies(health: dict, threshold: int = LIKELY_DEAD) -> list[str]:
    return [n for n, e in health.items() if e.get("consecutive_failures", 0) >= threshold]


def stale_companies(health: dict, hours: int = 72) -> list[str]:
    now = datetime.now(timezone.utc)
    stale = []
    for company, entry in health.items():
        last = entry.get("last_success")
        if not last:
            stale.append(company)
            continue
        try:
            if (now - datetime.fromisoformat(last)).total_seconds() > hours * 3600:
                stale.append(company)
        except ValueError:
            stale.append(company)
    return stale