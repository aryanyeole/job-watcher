"""
ATS discovery.

THE PROBLEM THIS SOLVES:
None of these ATS platforms offer a cross-company search endpoint. Greenhouse's
own docs say filtering and searching are not possible on the Job Board API —
you can only ask "what's open at company X". There is also no public directory
of which companies use which ATS, and it changes weekly.

So the company list IS the query, and a 16-company list is a 16-company query.
This module builds a list of THOUSANDS automatically: give it company names, it
probes each ATS for a matching public board and writes verified entries.

Run it periodically (monthly is plenty) to catch companies that switch ATS or
newly onboard.

Usage:
    python -m src.discover --input data/companies_seed.txt --output discovered.yaml
    python -m src.discover --input data/companies_seed.txt --merge companies.yaml
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PROBE_TIMEOUT = 10

# Discovery is a burst of many requests, so it's paced more conservatively
# than the normal polling loop. This runs rarely — slow is fine.
PROBE_DELAY = 0.15
MAX_WORKERS = 8


def slug_candidates(company_name: str) -> list[str]:
    """
    Generate plausible board tokens from a company name.

    "Scale AI"       -> ["scaleai", "scale-ai", "scale"]
    "Bloomberg L.P." -> ["bloomberglp", "bloomberg-lp", "bloomberg"]
    """
    name = company_name.strip().lower()
    # Drop common corporate suffixes.
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|lp|l\.p\.|plc|gmbh)\b\.?", "", name)
    cleaned = re.sub(r"[^a-z0-9\s-]", "", name).strip()

    if not cleaned:
        return []

    words = cleaned.split()
    candidates = [
        "".join(words),           # scaleai
        "-".join(words),          # scale-ai
    ]
    if len(words) > 1:
        candidates.append(words[0])   # scale

    # Dedupe, preserve order.
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _probe(url: str, expect_key: str | None = None, params: dict | None = None) -> int | None:
    """
    Return the number of jobs found, or None if this isn't a valid board.

    A board that exists but has zero open roles returns 0, which is still a
    valid discovery — the company uses this ATS and may post later.
    """
    try:
        resp = requests.get(
            url, params=params or {}, headers={"User-Agent": USER_AGENT}, timeout=PROBE_TIMEOUT
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:  # noqa: BLE001 — a failed probe just means "not this ATS"
        return None

    if expect_key:
        if not isinstance(data, dict) or expect_key not in data:
            return None
        return len(data.get(expect_key) or [])

    if isinstance(data, list):
        return len(data)
    return None


def probe_greenhouse(slug: str) -> int | None:
    return _probe(
        f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        expect_key="jobs",
        params={"content": "false"},
    )


def probe_lever(slug: str) -> int | None:
    return _probe(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})


def probe_ashby(slug: str) -> int | None:
    return _probe(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", expect_key="jobs")


def probe_smartrecruiters(slug: str) -> int | None:
    return _probe(
        f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
        expect_key="content",
        params={"limit": 1},
    )


PROBES = [
    ("greenhouse", probe_greenhouse),
    ("lever", probe_lever),
    ("ashby", probe_ashby),
    ("smartrecruiters", probe_smartrecruiters),
]


def discover_company(company_name: str) -> dict | None:
    """
    Find which ATS a company uses. Returns a companies.yaml entry, or None.

    ONLY accepts a board that actually returns at least one posting.

    Why: some ATS APIs (SmartRecruiters especially) answer HTTP 200 with an
    empty result set for ANY slug, including ones that don't exist. Treating
    an empty board as a valid discovery therefore produces a 100% "hit rate"
    made mostly of garbage — every unmatched company falls through to a
    phantom board that will never return anything.

    The trade-off is that a company genuinely on Greenhouse with zero open
    roles today won't be discovered. That's fine: re-run discovery monthly
    and it'll be picked up once they post something.
    """
    for slug in slug_candidates(company_name):
        for method, probe in PROBES:
            count = probe(slug)
            time.sleep(PROBE_DELAY)

            if count:  # None or 0 are both rejected
                return {
                    "name": company_name,
                    "method": method,
                    "slug": slug,
                    "tier": "bulk",
                    "_discovered_jobs": count,
                }

    return None


def discover_all(company_names: list[str], max_workers: int = MAX_WORKERS) -> list[dict]:
    found: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(discover_company, n): n for n in company_names}
        for i, future in enumerate(as_completed(futures), 1):
            name = futures[future]
            try:
                entry = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [err]  {name}: {exc}", file=sys.stderr)
                continue

            if entry:
                jobs = entry.pop("_discovered_jobs", 0)
                found.append(entry)
                print(f"  [{i}/{len(company_names)}] {name} -> {entry['method']}:{entry['slug']} ({jobs} jobs)")
            else:
                print(f"  [{i}/{len(company_names)}] {name} -> no public ATS board found")

    found.sort(key=lambda e: e["name"].lower())
    return found


def merge_into_config(entries: list[dict], config_path: str) -> tuple[int, int]:
    """
    Add newly-discovered entries to an existing companies.yaml, preserving
    everything already there (including manual `tier: hot` assignments and
    per-company keyword overrides).

    Returns (added, skipped).
    """
    path = Path(config_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    existing = config.get("companies", []) or []

    # Identity is (method, slug) for ATS boards, or the name for workday/custom.
    def identity(e: dict):
        if e.get("slug"):
            return (e.get("method"), e.get("slug"))
        return ("name", e.get("name", "").lower())

    known = {identity(e) for e in existing}

    added = 0
    for entry in entries:
        if identity(entry) in known:
            continue
        existing.append(entry)
        known.add(identity(entry))
        added += 1

    config["companies"] = existing
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )
    return added, len(entries) - added


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover which ATS each company uses")
    parser.add_argument("--input", required=True, help="text file, one company name per line")
    parser.add_argument("--output", help="write discovered entries to this YAML file")
    parser.add_argument("--merge", help="merge discovered entries into this existing config")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS)
    args = parser.parse_args()

    names = [
        line.strip()
        for line in Path(args.input).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    print(f"Probing {len(names)} companies across {len(PROBES)} ATS platforms...\n")

    entries = discover_all(names, args.workers)
    print(f"\nFound public boards for {len(entries)}/{len(names)} companies.")

    if args.output:
        Path(args.output).write_text(
            yaml.safe_dump({"companies": entries}, sort_keys=False, allow_unicode=True),
            encoding="utf-8"
        )
        print(f"Wrote {args.output}")

    if args.merge:
        added, skipped = merge_into_config(entries, args.merge)
        print(f"Merged into {args.merge}: {added} added, {skipped} already present.")

    if not args.output and not args.merge:
        print("\n(no --output or --merge given, nothing written)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
