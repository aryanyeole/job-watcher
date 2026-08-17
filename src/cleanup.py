"""
One-off cleanup for phantom discovery results.

The original discover.py accepted a board returning zero postings as a valid
find. SmartRecruiters answers HTTP 200 with an empty result set for ANY slug,
so every company not found on Greenhouse/Lever/Ashby fell through to a phantom
entry. That's why discovery reported an impossible 405/405.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

CONFIG_PATH = "companies.yaml"

METHOD_PRIORITY = {
    "custom_json": 0, "workday": 1, "playwright": 2,
    "greenhouse": 3, "lever": 4, "ashby": 5, "smartrecruiters": 9,
}


def entry_rank(entry: dict) -> tuple:
    return (
        METHOD_PRIORITY.get(entry.get("method"), 8),
        0 if entry.get("tier") == "hot" else 1,
    )


def clean(config_path: str = CONFIG_PATH) -> dict:
    path = Path(config_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    companies = config.get("companies", []) or []
    before = len(companies)

    dropped_sr = []
    kept = []
    for c in companies:
        if c.get("method") == "smartrecruiters":
            dropped_sr.append(c.get("name", "?"))
        else:
            kept.append(c)
    companies = kept

    by_name: dict[str, dict] = {}
    dropped_dupes = []
    for c in companies:
        key = (c.get("name") or "").strip().lower()
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = c
            continue
        if entry_rank(c) < entry_rank(existing):
            dropped_dupes.append(f"{existing.get('name')} ({existing.get('method')})")
            by_name[key] = c
        else:
            dropped_dupes.append(f"{c.get('name')} ({c.get('method')})")

    companies = list(by_name.values())
    companies.sort(key=lambda c: (0 if c.get("tier") == "hot" else 1, (c.get("name") or "").lower()))

    config["companies"] = companies
    path.write_text(
        yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )

    return {
        "before": before, "after": len(companies),
        "dropped_smartrecruiters": dropped_sr,
        "dropped_duplicates": dropped_dupes,
        "hot": sum(1 for c in companies if c.get("tier") == "hot"),
        "bulk": sum(1 for c in companies if c.get("tier") != "hot"),
    }


def main() -> int:
    r = clean()
    print(f"Removed {len(r['dropped_smartrecruiters'])} phantom SmartRecruiters entries.")
    if r["dropped_smartrecruiters"]:
        sample = ", ".join(r["dropped_smartrecruiters"][:12])
        more = len(r["dropped_smartrecruiters"]) - 12
        print(f"  e.g. {sample}{f' ... (+{more} more)' if more > 0 else ''}")
    print(f"\nRemoved {len(r['dropped_duplicates'])} duplicate entries.")
    for d in r["dropped_duplicates"][:12]:
        print(f"  - {d}")
    print(f"\ncompanies.yaml: {r['before']} -> {r['after']} entries")
    print(f"  tier hot:  {r['hot']}")
    print(f"  tier bulk: {r['bulk']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())