"""
Relevance filtering and signal extraction.

FILTERING POLICY — read this before changing anything here:

  The ONLY description-based reason a posting is ever dropped is a US
  CITIZENSHIP or SECURITY CLEARANCE requirement. Those roles are genuinely
  closed to an F-1/OPT candidate, so they're noise.

  Sponsorship is NEVER a filter. A company that won't sponsor H-1B can
  still hire you on OPT for up to three years — those are real, applyable
  jobs. Sponsorship language is detected and shown to you as an
  INFORMATIONAL TAG so you know what you're walking into, and that is all
  it does. There is deliberately no config option to filter on it, because
  the whole point is that you want to see those postings.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# HARD FILTER: citizenship / clearance requirements.
# These roles are closed to you on F-1/OPT, so they can be dropped.
# ---------------------------------------------------------------------------
CITIZENSHIP_REQUIRED_PATTERNS = [
    r"must\s+be\s+a?\s*u\.?\s?s\.?\s+citizen",
    r"u\.?\s?s\.?\s+citizenship\s+(?:is\s+)?(?:required|mandatory)",
    r"requires?\s+u\.?\s?s\.?\s+citizenship",
    r"restricted\s+to\s+u\.?\s?s\.?\s+citizens",
    r"only\s+u\.?\s?s\.?\s+citizens",
    r"u\.?\s?s\.?\s+citizens\s+only",
    # ITAR / export-control roles: "U.S. Person" excludes F-1 students.
    r"must\s+be\s+a\s+u\.?\s?s\.?\s+person",
    r"itar",
    r"export\s+control(?:led)?\s+regulations?",
]

CLEARANCE_REQUIRED_PATTERNS = [
    r"security\s+clearance",
    r"ts\s*/\s*sci",
    r"top\s+secret",
    r"active\s+secret\s+clearance",
    r"government\s+clearance",
    r"public\s+trust\s+clearance",
    r"polygraph",
]

# ---------------------------------------------------------------------------
# INFORMATIONAL ONLY — these NEVER cause a posting to be dropped.
# ---------------------------------------------------------------------------
NO_SPONSORSHIP_PATTERNS = [
    r"not\s+(?:be\s+)?(?:able\s+to\s+)?(?:offer|provide)\s+(?:visa\s+)?sponsorship",
    r"(?:will|do)\s+not\s+sponsor",
    r"unable\s+to\s+sponsor",
    r"no\s+visa\s+sponsorship",
    r"without\s+(?:the\s+)?need\s+for\s+(?:current\s+or\s+future\s+)?sponsorship",
    r"without\s+(?:current\s+or\s+future\s+)?(?:visa\s+)?sponsorship",
    r"not\s+require\s+sponsorship",
    r"does\s+not\s+provide\s+(?:visa\s+)?sponsorship",
    r"sponsorship\s+is\s+not\s+available",
    r"we\s+are\s+not\s+able\s+to\s+sponsor",
]

SPONSORSHIP_OK_PATTERNS = [
    r"will\s+sponsor",
    r"(?:offer|provide)s?\s+(?:visa\s+)?sponsorship",
    r"sponsorship\s+(?:is\s+)?available",
    r"open\s+to\s+sponsor",
    r"h-?1b\s+sponsorship",
]


def _matches_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def title_matches(
    title: str,
    keywords_any: list[str],
    keywords_none: list[str],
    keywords_role: list[str] | None = None,
) -> bool:
    """
    A title must satisfy THREE independent conditions:

      1. SENIORITY  - contains one of `keywords_any` ("new grad", "campus", ...)
      2. ROLE       - contains one of `keywords_role` ("software engineer", ...)
      3. EXCLUSION  - contains none of `keywords_none`

    Conditions 1 and 2 are separate dimensions on purpose. Seniority alone
    matches "Customer Experience Associate (New Grad)"; role alone matches
    "Principal Software Engineer". You need both to be true.
    """
    t = title.lower()

    if keywords_none and any(k.lower() in t for k in keywords_none):
        return False
    if keywords_any and not any(k.lower() in t for k in keywords_any):
        return False
    if keywords_role and not any(k.lower() in t for k in keywords_role):
        return False
    return True


def location_matches(location: str, allowed: list[str], blocked: list[str] | None = None) -> bool:
    """
    Empty `allowed` means every location passes.

    SHORT TOKENS ARE MATCHED ON WORD BOUNDARIES, not as raw substrings.

    Why this matters: a plain substring check makes "CA" match "Canada" and
    "US" match "Aarhus", so a Denmark req and a Canada req both sail through
    a US-only filter. Anything <= 3 characters is therefore matched with
    \\b...\\b; longer names ("San Francisco") stay as substrings so partial
    city/state phrasing still works.

    An `allowed` match WINS over a `blocked` match. "US and Canada Offices"
    is a US role and must pass; the cost is that "Remote - Canada" also slips
    through, which is the right trade — a stray alert is cheap, a missed req
    is not. `blocked` only decides locations that match nothing in `allowed`.
    """
    loc = (location or "").strip()

    # A posting with no location string shouldn't be silently dropped —
    # better a false alert than a missed req.
    if not loc:
        return True

    if allowed and any(_loc_token_match(loc, a) for a in allowed):
        return True

    if blocked and any(_loc_token_match(loc, b) for b in blocked):
        return False

    # Nothing in `allowed` matched. If `allowed` was set, that's a drop.
    return not allowed


def _loc_token_match(location: str, needle: str) -> bool:
    """Word-boundary match for short codes, substring match for longer names."""
    needle = needle.strip()
    if not needle:
        return False
    if len(needle) <= 3:
        return re.search(rf"\b{re.escape(needle)}\b", location, re.IGNORECASE) is not None
    return needle.lower() in location.lower()


def requires_citizenship(description: str) -> bool:
    """
    True only when the posting requires US citizenship, US-Person status, or
    a security clearance — the cases genuinely closed to you on OPT.

    Explicitly NOT triggered by any sponsorship language.
    """
    if not description:
        return False
    return _matches_any(description, CITIZENSHIP_REQUIRED_PATTERNS) or _matches_any(
        description, CLEARANCE_REQUIRED_PATTERNS
    )


def analyze(description: str) -> list[str]:
    """
    Build informational tags for a posting. Tags are DISPLAY ONLY — nothing
    here decides whether you see the job, except that the caller separately
    checks requires_citizenship().
    """
    if not description:
        return ["\u2753 no description"]

    flags: list[str] = []

    if _matches_any(description, CITIZENSHIP_REQUIRED_PATTERNS):
        flags.append("\U0001F1FA\U0001F1F8 US citizenship required")
    if _matches_any(description, CLEARANCE_REQUIRED_PATTERNS):
        flags.append("\U0001F512 clearance required")

    # Sponsorship: informational context, never a gate.
    # An explicit "no" beats generic company boilerplate about sponsoring.
    if _matches_any(description, NO_SPONSORSHIP_PATTERNS):
        flags.append("\u2139\uFE0F no H-1B sponsorship (OPT still fine)")
    elif _matches_any(description, SPONSORSHIP_OK_PATTERNS):
        flags.append("\u2705 sponsorship offered")

    return flags


def evaluate(job, company_cfg: dict, defaults: dict) -> tuple[bool, list[str]]:
    """
    Decide whether to alert on `job`, and compute its display flags.

    Drop conditions, in order:
      1. Title doesn't match your keywords.
      2. Location is outside your allowed list.
      3. Posting requires US citizenship / clearance (if enabled).

    Sponsorship status is NEVER a drop condition.

    Returns (should_alert, flags).
    """
    keywords_any = company_cfg.get("keywords_any", defaults.get("keywords_any_default", []))
    keywords_none = company_cfg.get("keywords_none", defaults.get("keywords_none_default", []))
    keywords_role = company_cfg.get("keywords_role", defaults.get("keywords_role_default", []))
    locations = company_cfg.get("locations", defaults.get("locations_default", []))
    locations_blocked = company_cfg.get(
        "locations_none", defaults.get("locations_none_default", [])
    )

    if not title_matches(job.title, keywords_any, keywords_none, keywords_role):
        return False, []
    if not location_matches(job.location, locations, locations_blocked):
        return False, []

    flags = analyze(job.description)

    return True, flags
