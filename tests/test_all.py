import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.filters import (
    analyze,
    evaluate,
    location_matches,
    requires_citizenship,
    title_matches,
)
from src.health import record_failure, record_success, stale_companies
from src.scrapers import Job, parse_workday_url, _dig, _strip_html
from src.state import load_state, save_state


# --------------------------- title filtering ---------------------------

def test_title_matches_keyword():
    assert title_matches("Software Engineer, New Grad", ["new grad"], ["senior"])


def test_title_excludes_blocked_keyword():
    assert not title_matches("Senior Software Engineer, New Grad", ["new grad"], ["senior"])


def test_title_case_insensitive():
    assert title_matches("SOFTWARE ENGINEER - NEW GRAD 2027", ["new grad"], [])


def test_title_empty_any_matches_everything():
    assert title_matches("Anything At All", [], [])


def test_google_early_career_title_matches():
    # The exact title Google uses — a plain "new grad" filter would MISS this,
    # which is precisely why keywords_any has several variants.
    assert title_matches(
        "Software Engineer, Early Career, Campus",
        ["new grad", "early career", "campus"],
        ["senior", "staff"],
    )


def test_intern_is_excluded():
    assert not title_matches(
        "Software Engineer Intern, Summer 2027", ["2027"], ["intern"]
    )


# --------------------------- location filtering ---------------------------

def test_location_allows_when_no_filter():
    assert location_matches("Belgrade, Serbia", [])


def test_location_blocks_non_matching():
    assert not location_matches("Belgrade, Serbia", ["United States", "Remote"])


def test_location_allows_matching():
    assert location_matches("San Francisco, CA", ["CA", "NY"])


def test_blank_location_passes_rather_than_being_dropped():
    # A missed req is worse than a false alert.
    assert location_matches("", ["United States"])


# --------------------- citizenship / clearance (HARD FILTER) ---------------------

@pytest.mark.parametrize(
    "text",
    [
        "Must be a U.S. citizen to be considered.",
        "US citizenship is required for this position.",
        "This role is restricted to U.S. citizens.",
        "Applicants must be a U.S. Person under ITAR regulations.",
        "Position is subject to export control regulations.",
    ],
)
def test_citizenship_requirement_is_detected(text):
    assert requires_citizenship(text)


@pytest.mark.parametrize(
    "text",
    [
        "Requires an active TS/SCI security clearance.",
        "Must hold an active Secret clearance.",
        "Candidates must pass a polygraph.",
    ],
)
def test_clearance_requirement_is_detected(text):
    assert requires_citizenship(text)


@pytest.mark.parametrize(
    "text",
    [
        "We will not sponsor applicants for work visas.",
        "This position does not provide visa sponsorship.",
        "Candidates must be authorized to work without future sponsorship.",
        "No visa sponsorship is offered for this position.",
    ],
)
def test_no_sponsorship_does_NOT_trigger_citizenship_filter(text):
    """THE KEY TEST: no-sponsorship roles are applyable on OPT and must survive."""
    assert not requires_citizenship(text)


def test_empty_description_does_not_trigger_citizenship_filter():
    assert not requires_citizenship("")


def test_ordinary_description_does_not_trigger_citizenship_filter():
    assert not requires_citizenship("Build backend services in Java, Kafka, and Redis.")


# --------------------- informational tags (DISPLAY ONLY) ---------------------

def test_no_sponsorship_tagged_as_opt_still_fine():
    flags = analyze("We will not sponsor applicants for employment visas.")
    assert any("no H-1B sponsorship" in f for f in flags)
    assert any("OPT still fine" in f for f in flags)


def test_sponsorship_offered_tagged():
    flags = analyze("We offer visa sponsorship for qualified candidates.")
    assert any("sponsorship offered" in f for f in flags)


def test_explicit_no_overrides_generic_yes_in_tags():
    text = (
        "Our company offers sponsorship across many roles. "
        "However, for this position we will not sponsor applicants."
    )
    flags = analyze(text)
    assert any("no H-1B sponsorship" in f for f in flags)
    assert not any("sponsorship offered" in f for f in flags)


def test_citizenship_tagged():
    assert any("citizenship required" in f for f in analyze("Must be a U.S. citizen."))


def test_missing_description_tagged_not_crashed():
    assert analyze("") == ["\u2753 no description"]


def test_neutral_description_gets_no_tags():
    assert analyze("Build scalable backend services in Java and Kafka.") == []


# --------------------------- evaluate() end to end ---------------------------

DEFAULTS = {
    "keywords_any_default": ["new grad", "early career"],
    "keywords_none_default": ["senior", "intern"],
    "locations_default": ["United States", "CA"],
    "drop_citizenship_required_default": True,
}


def _job(**kw):
    base = dict(id="x", title="Software Engineer, New Grad", url="u", company="C",
                location="San Francisco, CA", description="")
    base.update(kw)
    return Job(**base)


def test_evaluate_accepts_matching_job():
    ok, _ = evaluate(_job(), {}, DEFAULTS)
    assert ok


def test_evaluate_rejects_on_title():
    ok, _ = evaluate(_job(title="Senior Software Engineer"), {}, DEFAULTS)
    assert not ok


def test_evaluate_rejects_on_location():
    ok, _ = evaluate(_job(location="Belgrade, Serbia"), {}, DEFAULTS)
    assert not ok


def test_evaluate_KEEPS_job_with_no_sponsorship():
    """Non-sponsoring roles must come through — they're applyable on OPT."""
    ok, flags = evaluate(
        _job(description="We will not sponsor applicants for employment visas."),
        {}, DEFAULTS,
    )
    assert ok
    assert any("no H-1B sponsorship" in f for f in flags)


def test_evaluate_drops_citizenship_required():
    ok, _ = evaluate(_job(description="Must be a U.S. citizen."), {}, DEFAULTS)
    assert not ok


def test_evaluate_drops_clearance_required():
    ok, _ = evaluate(_job(description="Requires active TS/SCI clearance."), {}, DEFAULTS)
    assert not ok


def test_evaluate_keeps_citizenship_role_when_filter_disabled():
    cfg = {"drop_citizenship_required": False}
    ok, flags = evaluate(_job(description="Must be a U.S. citizen."), cfg, DEFAULTS)
    assert ok
    assert any("citizenship required" in f for f in flags)


def test_company_config_overrides_defaults():
    cfg = {"keywords_any": ["staff engineer"], "keywords_none": []}
    ok, _ = evaluate(_job(title="Staff Engineer, Platform"), cfg, DEFAULTS)
    assert ok


# --------------------------- Workday URL parsing ---------------------------

def test_parse_workday_nvidia():
    assert parse_workday_url(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
    ) == ("nvidia", "wd5", "NVIDIAExternalCareerSite")


def test_parse_workday_salesforce_different_shard():
    assert parse_workday_url(
        "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site"
    ) == ("salesforce", "wd12", "External_Career_Site")


def test_parse_workday_without_locale_segment():
    assert parse_workday_url(
        "https://adobe.wd5.myworkdayjobs.com/external_experienced"
    ) == ("adobe", "wd5", "external_experienced")


def test_parse_workday_rejects_non_workday_url():
    with pytest.raises(ValueError):
        parse_workday_url("https://boards.greenhouse.io/databricks")


# --------------------------- helpers ---------------------------

def test_dig_nested_path():
    assert _dig({"location": {"name": "SF"}}, "location.name") == "SF"


def test_dig_missing_path_returns_none():
    assert _dig({"a": 1}, "b.c") is None


def test_strip_html_removes_tags_and_entities():
    assert _strip_html("<p>Java &amp; Kafka</p>") == "Java & Kafka"


# --------------------------- state ---------------------------

def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    save_state(str(path), {"a", "b", "c"})
    assert load_state(str(path)) == {"a", "b", "c"}


def test_state_missing_file_returns_empty_set(tmp_path):
    assert load_state(str(tmp_path / "nope.json")) == set()


# --------------------------- health monitoring ---------------------------

def test_health_warns_when_board_drops_to_zero():
    health = {}
    record_success(health, "Databricks", 50)
    warnings = record_success(health, "Databricks", 0)
    assert warnings
    assert "0 jobs" in warnings[0]


def test_health_silent_when_board_stays_healthy():
    health = {}
    record_success(health, "Databricks", 50)
    assert record_success(health, "Databricks", 48) == []


def test_health_no_warning_on_first_ever_zero():
    # A board that has always been empty isn't evidence of breakage.
    health = {}
    assert record_success(health, "NewCo", 0) == []


def test_health_alerts_after_consecutive_failures():
    health = {}
    assert record_failure(health, "NVIDIA", "timeout") == []      # 1st: quiet
    warnings = record_failure(health, "NVIDIA", "timeout")        # 2nd: alert
    assert warnings


def test_health_success_resets_failure_counter():
    health = {}
    record_failure(health, "NVIDIA", "timeout")
    record_success(health, "NVIDIA", 10)
    assert health["NVIDIA"]["consecutive_failures"] == 0


def test_stale_companies_flags_never_succeeded():
    health = {"Ghost": {"consecutive_failures": 5}}
    assert "Ghost" in stale_companies(health)
