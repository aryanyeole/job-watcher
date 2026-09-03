"""
Prune dead boards from companies.yaml using health.json as evidence.

A slug that 404s isn't recoverable by re-running discovery: the merge step
skips anything already present by (method, slug), so a wrong slug stays wrong
forever. It has to be removed first, then rediscovered.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from src.health import load_health, save_health

CONFIG_PATH = "companies.yaml"
HEALTH_PATH = "health.json"
DEFAULT_MIN_FAILURES = 10


def prune(config_path=CONFIG_PATH, health_path=HEALTH_PATH,
          min_failures=DEFAULT_MIN_FAILURES, apply=False) -> dict:
    health = load_health(health_path)
    dead = {n: e for n, e in health.items()
            if e.get("consecutive_failures", 0) >= min_failures}

    path = Path(config_path)
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    companies = config.get("companies", []) or []

    removed, kept = [], []
    for c in companies:
        name = c.get("name", "")
        if name in dead:
            removed.append({
                "name": name, "method": c.get("method"),
                "slug": c.get("slug") or c.get("careers_url", ""),
                "failures": dead[name].get("consecutive_failures"),
                "error": dead[name].get("last_error", "")[:80],
                "was_hot": c.get("tier") == "hot",
            })
        else:
            kept.append(c)

    if apply and removed:
        config["companies"] = kept
        path.write_text(
            yaml.safe_dump(config, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        for e in removed:
            health.pop(e["name"], None)
        save_health(health_path, health)

    return {"removed": removed, "remaining": len(kept), "applied": apply}


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove dead boards")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-failures", type=int, default=DEFAULT_MIN_FAILURES)
    args = parser.parse_args()

    result = prune(min_failures=args.min_failures, apply=args.apply)

    if not result["removed"]:
        print(f"No boards with >= {args.min_failures} consecutive failures.")
        return 0

    verb = "Removed" if args.apply else "Would remove"
    print(f"{verb} {len(result['removed'])} dead board(s):\n")
    for e in result["removed"]:
        hot = " [WAS HOT]" if e["was_hot"] else ""
        print(f"  {e['name']}{hot}")
        print(f"    {e['method']}:{e['slug']}")
        print(f"    {e['failures']} failures — {e['error']}")
    print(f"\ncompanies.yaml would have {result['remaining']} entries.")
    if not args.apply:
        print("\nRe-run with --apply to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())