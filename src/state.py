"""Load/save the set of job IDs we've already seen, so re-runs only report new ones."""
from __future__ import annotations

import json
from pathlib import Path


def load_state(path: str) -> set[str]:
    p = Path(path)
    if not p.exists():
        return set()
    return set(json.loads(p.read_text()))


def save_state(path: str, seen_ids: set[str]) -> None:
    Path(path).write_text(json.dumps(sorted(seen_ids), indent=2))
