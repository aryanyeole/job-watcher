"""Tests for ATS discovery and tiered polling."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from src.discover import discover_company, merge_into_config, slug_candidates
from src.main import select_companies


# --------------------------- slug generation ---------------------------

def test_slug_candidates_single_word():
    assert slug_candidates("Databricks") == ["databricks"]


def test_slug_candidates_multi_word():
    cands = slug_candidates("Scale AI")
    assert "scaleai" in cands
    assert "scale-ai" in cands
    assert "scale" in cands


def test_slug_candidates_strips_corporate_suffix():
    cands = slug_candidates("Bloomberg L.P.")
    assert any("bloomberg" in c for c in cands)
    assert not any("l.p." in c for c in cands)


def test_slug_candidates_strips_punctuation():
    cands = slug_candidates("Checkout.com")
    assert all("." not in c for c in cands)


def test_slug_candidates_handles_empty():
    assert slug_candidates("   ") == []


def test_slug_candidates_deduplicates():
    cands = slug_candidates("Stripe")
    assert len(cands) == len(set(cands))


# --------------------------- discovery ---------------------------

def _resp(status, payload):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def test_discover_finds_greenhouse_board():
    def fake_get(url, **kwargs):
        if "greenhouse" in url and "testco" in url:
            return _resp(200, {"jobs": [{"id": 1}, {"id": 2}]})
        return _resp(404, {})

    with patch("src.discover.requests.get", side_effect=fake_get):
        entry = discover_company("TestCo")

    assert entry["method"] == "greenhouse"
    assert entry["slug"] == "testco"
    assert entry["tier"] == "bulk"


def test_discover_falls_through_to_lever():
    def fake_get(url, **kwargs):
        if "lever" in url:
            return _resp(200, [{"id": "a"}, {"id": "b"}])
        return _resp(404, {})

    with patch("src.discover.requests.get", side_effect=fake_get):
        entry = discover_company("TestCo")

    assert entry["method"] == "lever"


def test_discover_returns_none_when_no_board_exists():
    with patch("src.discover.requests.get", return_value=_resp(404, {})):
        assert discover_company("NoSuchCompany") is None


def test_discover_prefers_board_with_jobs_over_empty_one():
    """An empty board is a valid find, but a populated one wins."""
    def fake_get(url, **kwargs):
        if "greenhouse" in url:
            return _resp(200, {"jobs": []})       # exists but empty
        if "lever" in url:
            return _resp(200, [{"id": "a"}])      # has a real posting
        return _resp(404, {})

    with patch("src.discover.requests.get", side_effect=fake_get):
        entry = discover_company("TestCo")

    assert entry["method"] == "lever"


def test_discover_rejects_empty_board():
    """
    REGRESSION GUARD. SmartRecruiters returns HTTP 200 with an empty result
    set for ANY slug, including nonexistent ones. Accepting a 0-job board as
    a valid find produced a 100% "hit rate" made mostly of phantom entries.
    """
    def fake_get(url, **kwargs):
        if "smartrecruiters" in url:
            return _resp(200, {"content": [], "totalFound": 0})
        return _resp(404, {})

    with patch("src.discover.requests.get", side_effect=fake_get):
        assert discover_company("NotARealCompany") is None


def test_discover_rejects_empty_greenhouse_board_too():
    def fake_get(url, **kwargs):
        if "greenhouse" in url:
            return _resp(200, {"jobs": []})
        return _resp(404, {})

    with patch("src.discover.requests.get", side_effect=fake_get):
        assert discover_company("TestCo") is None


def test_discover_survives_network_error():
    with patch("src.discover.requests.get", side_effect=ConnectionError("boom")):
        assert discover_company("TestCo") is None


def test_discover_ignores_malformed_json():
    r = MagicMock()
    r.status_code = 200
    r.json.side_effect = ValueError("not json")
    with patch("src.discover.requests.get", return_value=r):
        assert discover_company("TestCo") is None


# --------------------------- config merging ---------------------------

def test_merge_adds_new_entries(tmp_path):
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(yaml.safe_dump({
        "companies": [{"name": "Existing", "method": "greenhouse", "slug": "existing"}],
        "filters": {"keywords_any_default": ["new grad"]},
    }))

    added, skipped = merge_into_config(
        [{"name": "NewCo", "method": "lever", "slug": "newco", "tier": "bulk"}], str(cfg)
    )

    assert (added, skipped) == (1, 0)
    result = yaml.safe_load(cfg.read_text())
    assert len(result["companies"]) == 2


def test_merge_does_not_duplicate_existing(tmp_path):
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(yaml.safe_dump({
        "companies": [{"name": "Existing", "method": "greenhouse", "slug": "existing"}],
    }))

    added, skipped = merge_into_config(
        [{"name": "Existing", "method": "greenhouse", "slug": "existing", "tier": "bulk"}], str(cfg)
    )

    assert (added, skipped) == (0, 1)
    assert len(yaml.safe_load(cfg.read_text())["companies"]) == 1


def test_merge_preserves_hot_tier_and_overrides(tmp_path):
    """Re-running discovery must not clobber your manual tier/keyword tuning."""
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(yaml.safe_dump({
        "companies": [{
            "name": "Databricks", "method": "greenhouse", "slug": "databricks",
            "tier": "hot", "keywords_any": ["custom"],
        }],
        "filters": {"keywords_any_default": ["new grad"]},
    }))

    merge_into_config(
        [{"name": "Databricks", "method": "greenhouse", "slug": "databricks", "tier": "bulk"}],
        str(cfg),
    )

    entry = yaml.safe_load(cfg.read_text())["companies"][0]
    assert entry["tier"] == "hot"
    assert entry["keywords_any"] == ["custom"]


def test_merge_preserves_filters_block(tmp_path):
    cfg = tmp_path / "companies.yaml"
    cfg.write_text(yaml.safe_dump({
        "companies": [],
        "filters": {"keywords_any_default": ["new grad"], "drop_citizenship_required_default": True},
    }))

    merge_into_config([{"name": "X", "method": "lever", "slug": "x", "tier": "bulk"}], str(cfg))

    result = yaml.safe_load(cfg.read_text())
    assert result["filters"]["drop_citizenship_required_default"] is True


def test_merge_into_missing_file_creates_it(tmp_path):
    cfg = tmp_path / "new.yaml"
    added, _ = merge_into_config([{"name": "X", "method": "lever", "slug": "x"}], str(cfg))
    assert added == 1
    assert cfg.exists()


# --------------------------- tier selection ---------------------------

COMPANIES = [
    {"name": "Hot1", "tier": "hot"},
    {"name": "Hot2", "tier": "hot"},
    {"name": "Bulk1", "tier": "bulk"},
    {"name": "NoTier"},  # defaults to bulk
]


def test_select_hot_tier():
    assert [c["name"] for c in select_companies(COMPANIES, "hot")] == ["Hot1", "Hot2"]


def test_select_bulk_includes_untiered_entries():
    """Discovered companies omit `tier`, so the default must be bulk."""
    names = [c["name"] for c in select_companies(COMPANIES, "bulk")]
    assert names == ["Bulk1", "NoTier"]


def test_select_all_returns_everything():
    assert len(select_companies(COMPANIES, "all")) == 4


def test_select_empty_tier_returns_nothing():
    assert select_companies([{"name": "X", "tier": "bulk"}], "hot") == []
