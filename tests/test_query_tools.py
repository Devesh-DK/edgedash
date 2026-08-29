"""
tests/test_query_tools.py — unit tests for edgedash/query/tools.py.

Every storage call is patched so these tests run without a real database.
No LLM. No network. No file I/O.

Covered cases (as specified):
  A. @tool decorator / TOOLS registry
       1. All 7 tools are registered with the required fields.
       2. call() raises KeyError for an unknown tool name.
       3. call() routes to the correct underlying function.

  B. Clamping — every parameter is validated against both bounds.
       4.  days  clamped at lower bound  (0 → 1)
       5.  days  clamped at upper bound  (999 → 90)
       6.  n     clamped at lower bound  (0 → 1)
       7.  n     clamped at upper bound  (999 → 25)
       8.  weeks clamped at lower bound  (0 → 1)
       9.  weeks clamped at upper bound  (999 → 12)
       10. non-integer days string        ("abc" → default 7)

  C. Return shape — every tool returns ToolResult with list[dict] + str.
       11. companies_hiring  returns correct shape.
       12. best_matches      returns correct shape.
       13. top_gaps          returns correct shape.
       14. gap_detail        returns correct shape.
       15. trend             returns correct shape.
       16. listing_count     returns correct shape.
       17. skill_demand      returns correct shape.

  D. Unknown / missing skill returns empty rather than raising.
       18. gap_detail with unknown skill  → rows=[], no exception.
       19. skill_demand with unknown skill → rows=[], no exception.
       20. gap_detail with None skill      → rows=[], no exception.
       21. skill_demand with empty string  → rows=[], no exception.

  E. Edge cases.
       22. top_gaps respects n clamp: only returns min(n, available) rows.
       23. best_matches respects n clamp.
       24. trend with no snapshots returns summary message, empty rows.
       25. trend with one snapshot returns summary message, empty rows.
       26. listing_count always returns exactly one row.
       27. companies_hiring summary mentions the clamped days value.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from edgedash.query.tools import (
    TOOLS,
    ToolResult,
    _clamp_days,
    _clamp_n,
    _clamp_weeks,
    call,
    companies_hiring,
    best_matches,
    top_gaps,
    gap_detail,
    trend,
    listing_count,
    skill_demand,
)

# ---------------------------------------------------------------------------
# Shared fake data
# ---------------------------------------------------------------------------

_FAKE_COMPANIES = [
    {"company": "Acme Corp",   "listing_count": 5, "newest_posted_at": "2026-08-20"},
    {"company": "Beta Ltd",    "listing_count": 2, "newest_posted_at": "2026-08-19"},
]

_FAKE_LISTINGS = [
    {"id": "a1", "title": "Data Analyst", "company": "Acme Corp",
     "fit_score": 82, "fit_reason": "3/4 skills matched", "location": "Remote",
     "posted_at": "2026-08-20", "fetched_at": "2026-08-20"},
    {"id": "a2", "title": "BI Developer",  "company": "Beta Ltd",
     "fit_score": 71, "fit_reason": "2/4 skills matched", "location": "Bengaluru",
     "posted_at": "2026-08-18", "fetched_at": "2026-08-18"},
]

_FAKE_GAPS = [
    {"skill": "kubernetes", "listings_blocked": 8, "opportunity_cost": 6.2,
     "mean_score": 77.5, "top_score": 85, "example_ids": ["a1", "a2"],
     "also_nice_to_have": 2, "low_confidence": False, "computed_at": "2026-08-20T10:00:00"},
    {"skill": "terraform",  "listings_blocked": 4, "opportunity_cost": 3.1,
     "mean_score": 78.0, "top_score": 82, "example_ids": ["a1"],
     "also_nice_to_have": 1, "low_confidence": False, "computed_at": "2026-08-20T10:00:00"},
    {"skill": "dbt",        "listings_blocked": 3, "opportunity_cost": 2.4,
     "mean_score": 80.0, "top_score": 82, "example_ids": ["a2"],
     "also_nice_to_have": 0, "low_confidence": True,  "computed_at": "2026-08-20T10:00:00"},
]

_FAKE_STATS = {
    "total": 47,
    "scored": 40,
    "unscored": 7,
    "newest_posted_at": "2026-08-20",
}

_FAKE_SKILL_DEMAND_ROWS = [
    {"id": "a1", "title": "Data Analyst", "company": "Acme Corp",
     "fit_score": 82, "in_required": True, "in_nice_to_have": False},
    {"id": "a2", "title": "BI Developer",  "company": "Beta Ltd",
     "fit_score": 71, "in_required": False, "in_nice_to_have": True},
]

_FAKE_RUN_IDS = [
    {"run_id": "run_1", "computed_at": "2026-08-13T10:00:00"},
    {"run_id": "run_2", "computed_at": "2026-08-20T10:00:00"},
]

_FAKE_TREND_DATA = {
    "run_1": [
        {"skill": "kubernetes", "listings_blocked": 6, "opportunity_cost": 4.8,
         "mean_score": 80.0, "top_score": 85, "example_ids": ["a1"],
         "also_nice_to_have": 1, "low_confidence": False, "computed_at": "2026-08-13T10:00:00"},
    ],
    "run_2": _FAKE_GAPS,
}


# ---------------------------------------------------------------------------
# Helper: assert a ToolResult has the right shape
# ---------------------------------------------------------------------------

def _assert_tool_result(result: object) -> None:
    assert isinstance(result, ToolResult), f"Expected ToolResult, got {type(result)}"
    assert isinstance(result.rows, list),  "rows must be a list"
    assert isinstance(result.summary, str), "summary must be a str"
    assert len(result.summary) > 0,         "summary must not be empty"


# ---------------------------------------------------------------------------
# A. Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    EXPECTED_TOOLS = {
        "companies_hiring",
        "best_matches",
        "top_gaps",
        "gap_detail",
        "trend",
        "listing_count",
        "skill_demand",
    }

    def test_all_seven_tools_registered(self) -> None:
        """All 7 tools must be present in the TOOLS registry."""
        assert self.EXPECTED_TOOLS == set(TOOLS.keys()), (
            f"Missing: {self.EXPECTED_TOOLS - set(TOOLS.keys())}"
        )

    def test_each_tool_has_required_fields(self) -> None:
        """Every ToolSpec must have name, description, parameters, and fn."""
        for name, spec in TOOLS.items():
            assert spec.name == name,              f"{name}: spec.name mismatch"
            assert isinstance(spec.description, str) and spec.description, \
                f"{name}: empty description"
            assert isinstance(spec.parameters, list), \
                f"{name}: parameters must be a list"
            assert callable(spec.fn),              f"{name}: fn must be callable"

    def test_call_unknown_tool_raises_key_error(self) -> None:
        """call() must raise KeyError for a tool name not in the registry."""
        with pytest.raises(KeyError, match="Unknown tool"):
            call("does_not_exist")

    def test_call_routes_to_correct_function(self) -> None:
        """call('listing_count') must reach listing_count() — no DB needed."""
        with patch("edgedash.query.tools.storage.get_listing_stats",
                   return_value=_FAKE_STATS):
            result = call("listing_count")
        _assert_tool_result(result)
        assert result.rows == [_FAKE_STATS]


# ---------------------------------------------------------------------------
# B. Clamping
# ---------------------------------------------------------------------------

class TestClamping:
    # days
    def test_days_clamp_lower(self) -> None:
        assert _clamp_days(0) == 1

    def test_days_clamp_upper(self) -> None:
        assert _clamp_days(999) == 90

    def test_days_within_range(self) -> None:
        assert _clamp_days(30) == 30

    # n
    def test_n_clamp_lower(self) -> None:
        assert _clamp_n(0) == 1

    def test_n_clamp_upper(self) -> None:
        assert _clamp_n(999) == 25

    def test_n_within_range(self) -> None:
        assert _clamp_n(10) == 10

    # weeks
    def test_weeks_clamp_lower(self) -> None:
        assert _clamp_weeks(0) == 1

    def test_weeks_clamp_upper(self) -> None:
        assert _clamp_weeks(999) == 12

    def test_weeks_within_range(self) -> None:
        assert _clamp_weeks(6) == 6

    # non-integer string falls back to default
    def test_non_integer_days_uses_default(self) -> None:
        assert _clamp_days("abc") == 7

    def test_non_integer_n_uses_default(self) -> None:
        assert _clamp_n("abc") == 10

    def test_non_integer_weeks_uses_default(self) -> None:
        assert _clamp_weeks("abc") == 3

    # clamping is applied end-to-end inside companies_hiring
    def test_companies_hiring_clamps_days_lower_bound(self) -> None:
        """days=0 must be clamped to 1 — summary must say '1 day'."""
        with patch("edgedash.query.tools.storage.get_companies_hiring",
                   return_value=_FAKE_COMPANIES) as mock_ch, \
             patch("edgedash.query.tools.storage.count_listings_in_window",
                   return_value=7):
            result = companies_hiring(days=0)
        _assert_tool_result(result)
        assert "1 day" in result.summary

    def test_companies_hiring_clamps_days_upper_bound(self) -> None:
        """days=9999 must be clamped to 90 — summary must say '90 days'."""
        with patch("edgedash.query.tools.storage.get_companies_hiring",
                   return_value=_FAKE_COMPANIES), \
             patch("edgedash.query.tools.storage.count_listings_in_window",
                   return_value=7):
            result = companies_hiring(days=9999)
        _assert_tool_result(result)
        assert "90 days" in result.summary

    def test_best_matches_clamps_n_upper_bound(self) -> None:
        """n=999 must be clamped to 25; storage is called with limit=25."""
        with patch("edgedash.query.tools.storage.get_listings",
                   return_value=_FAKE_LISTINGS) as mock_gl:
            best_matches(n=999)
        mock_gl.assert_called_once_with(limit=25, min_score=0)

    def test_best_matches_clamps_n_lower_bound(self) -> None:
        """n=0 must be clamped to 1; storage is called with limit=1."""
        with patch("edgedash.query.tools.storage.get_listings",
                   return_value=[_FAKE_LISTINGS[0]]) as mock_gl:
            best_matches(n=0)
        mock_gl.assert_called_once_with(limit=1, min_score=0)


# ---------------------------------------------------------------------------
# C. Return shape
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_companies_hiring_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_companies_hiring",
                   return_value=_FAKE_COMPANIES), \
             patch("edgedash.query.tools.storage.count_listings_in_window",
                   return_value=7):
            result = companies_hiring(days=7)
        _assert_tool_result(result)
        assert result.rows == _FAKE_COMPANIES
        for row in result.rows:
            assert "company"       in row
            assert "listing_count" in row

    def test_best_matches_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_listings",
                   return_value=_FAKE_LISTINGS):
            result = best_matches(n=10)
        _assert_tool_result(result)
        for row in result.rows:
            assert "fit_score" in row
            assert "title"     in row
            assert "company"   in row

    def test_top_gaps_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_latest_gap_snapshot",
                   return_value=_FAKE_GAPS):
            result = top_gaps(n=5)
        _assert_tool_result(result)
        for row in result.rows:
            assert "skill"            in row
            assert "listings_blocked" in row
            assert "opportunity_cost" in row

    def test_gap_detail_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value={"kubernetes", "terraform"}), \
             patch("edgedash.query.tools.storage.get_latest_gap_snapshot",
                   return_value=_FAKE_GAPS), \
             patch("edgedash.query.tools.storage.get_listings",
                   return_value=_FAKE_LISTINGS), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = gap_detail(skill="kubernetes")
        _assert_tool_result(result)

    def test_trend_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_all_run_ids",
                   return_value=_FAKE_RUN_IDS), \
             patch("edgedash.query.tools.storage.get_gap_snapshots_for_trend",
                   return_value=_FAKE_TREND_DATA):
            result = trend(weeks=3)
        _assert_tool_result(result)
        for row in result.rows:
            assert "skill"        in row
            assert "latest_cost"  in row
            assert "delta_abs"    in row
            assert "is_new"       in row

    def test_listing_count_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_listing_stats",
                   return_value=_FAKE_STATS):
            result = listing_count()
        _assert_tool_result(result)
        assert len(result.rows) == 1
        row = result.rows[0]
        assert "total"            in row
        assert "scored"           in row
        assert "unscored"         in row
        assert "newest_posted_at" in row

    def test_skill_demand_shape(self) -> None:
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value={"python", "sql"}), \
             patch("edgedash.query.tools.storage.get_skill_demand",
                   return_value=_FAKE_SKILL_DEMAND_ROWS), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = skill_demand(skill="python")
        _assert_tool_result(result)
        for row in result.rows:
            assert "id"              in row
            assert "in_required"     in row
            assert "in_nice_to_have" in row


# ---------------------------------------------------------------------------
# D. Unknown / missing skill → empty rows, no exception
# ---------------------------------------------------------------------------

class TestUnknownSkill:
    def _patch_no_skills(self) -> dict:
        return {"edgedash.query.tools.storage.get_skills_present_in_db": set()}

    def test_gap_detail_unknown_skill_returns_empty(self) -> None:
        """An unrecognised skill must return rows=[] without raising."""
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value=set()), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = gap_detail(skill="unknownskillxyz")
        assert isinstance(result, ToolResult)
        assert result.rows == []
        assert isinstance(result.summary, str)

    def test_skill_demand_unknown_skill_returns_empty(self) -> None:
        """An unrecognised skill must return rows=[] without raising."""
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value=set()), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = skill_demand(skill="unknownskillxyz")
        assert isinstance(result, ToolResult)
        assert result.rows == []

    def test_gap_detail_none_skill_returns_empty(self) -> None:
        """None as skill must return rows=[] without raising."""
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value=set()), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = gap_detail(skill=None)
        assert result.rows == []

    def test_skill_demand_empty_string_returns_empty(self) -> None:
        """Empty string as skill must return rows=[] without raising."""
        with patch("edgedash.query.tools.storage.get_skills_present_in_db",
                   return_value=set()), \
             patch("edgedash.query.tools.load_config") as mock_cfg:
            mock_cfg.return_value.skill_aliases = {}
            result = skill_demand(skill="")
        assert result.rows == []


# ---------------------------------------------------------------------------
# E. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_top_gaps_respects_n_clamp(self) -> None:
        """top_gaps(n=1) must return only 1 row even when 3 are available."""
        with patch("edgedash.query.tools.storage.get_latest_gap_snapshot",
                   return_value=_FAKE_GAPS):
            result = top_gaps(n=1)
        assert len(result.rows) == 1
        assert result.rows[0]["skill"] == "kubernetes"

    def test_top_gaps_n_larger_than_available(self) -> None:
        """top_gaps(n=25) with only 3 rows must return all 3, not crash."""
        with patch("edgedash.query.tools.storage.get_latest_gap_snapshot",
                   return_value=_FAKE_GAPS):
            result = top_gaps(n=25)
        assert len(result.rows) == 3

    def test_best_matches_fewer_than_n_available(self) -> None:
        """best_matches returns however many the DB gives, not n rows exactly."""
        with patch("edgedash.query.tools.storage.get_listings",
                   return_value=_FAKE_LISTINGS):
            result = best_matches(n=10)
        assert len(result.rows) == len(_FAKE_LISTINGS)

    def test_trend_no_snapshots(self) -> None:
        """trend() with no runs must return empty rows and informative summary."""
        with patch("edgedash.query.tools.storage.get_all_run_ids",
                   return_value=[]):
            result = trend(weeks=3)
        assert result.rows == []
        assert "No gap snapshots" in result.summary

    def test_trend_single_snapshot(self) -> None:
        """trend() with one run must return empty rows and informative summary."""
        with patch("edgedash.query.tools.storage.get_all_run_ids",
                   return_value=[_FAKE_RUN_IDS[0]]):
            result = trend(weeks=3)
        assert result.rows == []
        assert "one snapshot" in result.summary.lower()

    def test_listing_count_always_one_row(self) -> None:
        """listing_count always returns exactly one row regardless of DB state."""
        with patch("edgedash.query.tools.storage.get_listing_stats",
                   return_value=_FAKE_STATS):
            result = listing_count()
        assert len(result.rows) == 1

    def test_companies_hiring_summary_mentions_days(self) -> None:
        """The summary string must contain the clamped days value used."""
        with patch("edgedash.query.tools.storage.get_companies_hiring",
                   return_value=_FAKE_COMPANIES), \
             patch("edgedash.query.tools.storage.count_listings_in_window",
                   return_value=14):
            result = companies_hiring(days=14)
        assert "14 day" in result.summary

    def test_companies_hiring_empty_result(self) -> None:
        """companies_hiring with zero results must return empty rows cleanly."""
        with patch("edgedash.query.tools.storage.get_companies_hiring",
                   return_value=[]), \
             patch("edgedash.query.tools.storage.count_listings_in_window",
                   return_value=0):
            result = companies_hiring(days=7)
        assert result.rows == []
        _assert_tool_result(result)

    def test_top_gaps_empty_snapshot(self) -> None:
        """top_gaps with no snapshot data must return empty rows cleanly."""
        with patch("edgedash.query.tools.storage.get_latest_gap_snapshot",
                   return_value=[]):
            result = top_gaps(n=5)
        assert result.rows == []
        _assert_tool_result(result)
