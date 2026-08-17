"""
End-to-end test of the scrape -> filter -> diff -> notify pipeline with the
network mocked out. This is what proves the wiring works, not just the
individual functions.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import yaml

from src import main as main_mod


GREENHOUSE_RESPONSE = {
    "jobs": [
        {
            "id": 1001,
            "title": "Software Engineer - New Grad (2026 Start)",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1001",
            "updated_at": "2026-08-15T10:00:00Z",
            "content": "<p>Build distributed systems. We offer visa sponsorship.</p>",
        },
        {
            "id": 1002,
            "title": "Senior Staff Engineer",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1002",
            "updated_at": "2026-08-15T10:00:00Z",
            "content": "<p>10+ years experience.</p>",
        },
        {
            "id": 1003,
            "title": "Software Engineer, Early Career",
            "location": {"name": "Belgrade, Serbia"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1003",
            "updated_at": "2026-08-15T10:00:00Z",
            "content": "<p>EU role.</p>",
        },
        {
            "id": 1004,
            "title": "New Grad Software Engineer",
            "location": {"name": "Austin, TX"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1004",
            "updated_at": "2026-08-15T10:00:00Z",
            "content": "<p>We will not sponsor applicants for this role.</p>",
        },
        {
            "id": 1005,
            "title": "New Grad Software Engineer, Defense Systems",
            "location": {"name": "San Francisco, CA"},
            "absolute_url": "https://boards.greenhouse.io/testco/jobs/1005",
            "updated_at": "2026-08-15T10:00:00Z",
            "content": "<p>Must be a U.S. citizen with active security clearance.</p>",
        },
    ]
}

CONFIG = {
    "companies": [{"name": "TestCo", "method": "greenhouse", "slug": "testco"}],
    "filters": {
        "keywords_any_default": ["new grad", "early career"],
        "keywords_none_default": ["senior", "staff", "intern"],
        "locations_default": ["CA", "TX", "United States"],
        "drop_citizenship_required_default": True,
    },
}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated working directory with a config file."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "companies.yaml").write_text(yaml.safe_dump(CONFIG))
    return tmp_path


def _mock_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_full_pipeline_filters_correctly(workspace, capsys):
    """Of 4 postings, only 2 should survive title + location filtering."""
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out

    assert "Software Engineer - New Grad (2026 Start)" in out  # matches
    assert "New Grad Software Engineer" in out                  # matches (flagged)
    assert "Senior Staff Engineer" not in out                   # title excluded
    assert "Early Career" not in out or "Belgrade" not in out   # location excluded
    assert "2 new & relevant" in out


def test_non_sponsoring_job_is_KEPT_not_filtered(workspace, capsys):
    """Non-sponsoring roles are applyable on OPT and must reach the alert."""
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out
    assert "New Grad Software Engineer" in out            # job 1004 present
    assert "no H-1B sponsorship" in out                    # tagged, not dropped
    assert "sponsorship offered" in out                    # job 1001 tagged


def test_citizenship_required_job_is_dropped(workspace, capsys):
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out
    assert "Defense Systems" not in out


def test_second_run_reports_nothing_new(workspace, capsys, monkeypatch):
    """The whole point of state.json: no duplicate alerts."""
    monkeypatch.setattr("src.notify.send", lambda *a, **k: None)
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=False)
        capsys.readouterr()
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out
    assert "0 new & relevant" in out
    assert "No new matching postings." in out


def test_newly_added_posting_is_detected(workspace, capsys, monkeypatch):
    """A posting that appears on run 2 must be reported."""
    monkeypatch.setattr("src.notify.send", lambda *a, **k: None)
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=False)
        capsys.readouterr()

    updated = json.loads(json.dumps(GREENHOUSE_RESPONSE))
    updated["jobs"].append({
        "id": 9999,
        "title": "Software Engineer, New Grad - Backend",
        "location": {"name": "Seattle, WA"},
        "absolute_url": "https://boards.greenhouse.io/testco/jobs/9999",
        "updated_at": "2026-08-16T09:00:00Z",
        "content": "<p>Kafka and Spring Boot.</p>",
    })
    CONFIG["filters"]["locations_default"].append("WA")
    (workspace / "companies.yaml").write_text(yaml.safe_dump(CONFIG))

    with patch("src.scrapers.requests.request", return_value=_mock_response(updated)):
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out
    assert "Software Engineer, New Grad - Backend" in out
    assert "1 new & relevant" in out


def test_seed_mode_records_without_alerting(workspace, capsys):
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(seed=True)

    out = capsys.readouterr().out
    assert "SEED complete" in out
    assert (workspace / "state.json").exists()

    # After seeding, a normal run finds nothing new.
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=True)
    assert "0 new & relevant" in capsys.readouterr().out


def test_one_broken_company_does_not_kill_the_run(workspace, capsys):
    """A failing board must not prevent other boards from being checked."""
    cfg = {
        "companies": [
            {"name": "BrokenCo", "method": "greenhouse", "slug": "broken"},
            {"name": "TestCo", "method": "greenhouse", "slug": "testco"},
        ],
        "filters": CONFIG["filters"],
    }
    (workspace / "companies.yaml").write_text(yaml.safe_dump(cfg))

    def side_effect(method, url, **kwargs):
        if "broken" in url:
            raise ConnectionError("board is down")
        return _mock_response(GREENHOUSE_RESPONSE)

    with patch("src.scrapers.requests.request", side_effect=side_effect):
        main_mod.run(dry_run=True)

    out = capsys.readouterr().out
    assert "TestCo" in out                     # healthy board still scraped
    assert "new & relevant" in out


def test_history_csv_is_written(workspace, monkeypatch):
    monkeypatch.setattr("src.notify.send", lambda *a, **k: None)
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=False)

    history = workspace / "history.csv"
    assert history.exists()
    text = history.read_text(encoding="utf-8")
    assert "detected_at,company,title" in text
    assert "TestCo" in text


def test_health_file_written(workspace, monkeypatch):
    monkeypatch.setattr("src.notify.send", lambda *a, **k: None)
    with patch("src.scrapers.requests.request", return_value=_mock_response(GREENHOUSE_RESPONSE)):
        main_mod.run(dry_run=False)

    health = json.loads((workspace / "health.json").read_text(encoding="utf-8"))
    assert health["TestCo"]["last_job_count"] == 5
    assert health["TestCo"]["consecutive_failures"] == 0
