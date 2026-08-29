"""
Tests for edgedash/verification.py.

One passing case, one failing case, and any notable edge case per check.
No I/O, no network, no database — all pure function calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace

import pytest

from edgedash.config import Config
from edgedash.verification import (
    CheckResult,
    Verdict,
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# Minimal Config fixture — only the verification thresholds matter here.
# We construct one directly so tests don't depend on config.yaml on disk.
# ---------------------------------------------------------------------------

BASE_CFG = Config(
    target_role="Data Analyst",
    target_city="Remote",
    target_seniority="mid",
    keywords=[],
    my_skills=[],
    experience_years=0,
    db_path=":memory:",
    min_fit_score=60,
    sources=[],
    use_mock_fetcher=False,
    llm_provider="gemini",
    llm_model="gemini-3.5-flash-lite",
    llm_requests_per_second=1,
    llm_requests_per_minute=15,
    scoring_batch_size=25,
    fetch_interval_hours=6,
    fetch_max_pages=5,
    fetch_max_listings=200,
    score_max_seconds=300,
    analyse_max_seconds=120,
    weight_skill_match=0.45,
    weight_seniority_fit=0.25,
    weight_location_fit=0.15,
    weight_recency=0.15,
    skill_aliases={},
    min_score_spread=10.0,
    min_score_stdev=5.0,
    max_empty_extraction_pct=20.0,
    max_skills_per_listing=100,
    min_gap_sample=3,
    max_data_age_days=3,
)

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# check_score_spread
# ---------------------------------------------------------------------------

class TestCheckScoreSpread:
    def test_pass_healthy_distribution(self) -> None:
        # Scores with spread=60 and stdev well above 5
        scores = [20.0, 40.0, 55.0, 70.0, 80.0]
        result = check_score_spread(scores, BASE_CFG)
        assert result.passed is True
        assert result.name == "check_score_spread"
        assert "spread" in result.observed

    def test_fail_spread_too_small(self) -> None:
        # max-min = 5, below threshold of 10
        scores = [70.0, 71.0, 73.0, 74.0, 75.0]
        result = check_score_spread(scores, BASE_CFG)
        assert result.passed is False
        assert "spread" in result.observed
        assert "inflation" in result.message.lower() or "threshold" in result.message.lower()

    def test_fail_stdev_too_small(self) -> None:
        # Spread passes (>10) but stdev is tiny — all scores near same value
        # 50, 51, 50, 51, 61 → spread=11 but stdev ≈ 4.4
        scores = [50.0, 51.0, 50.0, 51.0, 61.0]
        result = check_score_spread(scores, BASE_CFG)
        assert result.passed is False
        assert "stdev" in result.observed

    def test_trivial_pass_fewer_than_5(self) -> None:
        scores = [40.0, 80.0]
        result = check_score_spread(scores, BASE_CFG)
        assert result.passed is True
        assert "trivial" in result.message.lower()
        assert "n=2" in result.observed

    def test_trivial_pass_empty_list(self) -> None:
        result = check_score_spread([], BASE_CFG)
        assert result.passed is True
        assert "n=0" in result.observed

    def test_pass_exactly_5_scores(self) -> None:
        # Boundary: exactly 5 scores should be evaluated (not trivial pass)
        scores = [10.0, 30.0, 50.0, 70.0, 90.0]
        result = check_score_spread(scores, BASE_CFG)
        assert result.passed is True
        assert "trivial" not in result.message.lower()


# ---------------------------------------------------------------------------
# check_extraction_sanity
# ---------------------------------------------------------------------------

class TestCheckExtractionSanity:
    def _make_facts(self, skills_per_listing: list[list[str]]) -> list[dict]:
        return [{"required_skills": s} for s in skills_per_listing]

    def test_pass_healthy_extractions(self) -> None:
        facts = self._make_facts([
            ["Python", "SQL"],
            ["Tableau", "Excel"],
            ["pandas", "NumPy"],
            ["ETL", "data pipeline"],
            ["Power BI"],
        ])
        result = check_extraction_sanity(facts, BASE_CFG)
        assert result.passed is True

    def test_fail_too_many_empty(self) -> None:
        # 3 of 5 empty → 60 %, above 20 % threshold
        facts = self._make_facts([[], [], [], ["Python"], ["SQL"]])
        result = check_extraction_sanity(facts, BASE_CFG)
        assert result.passed is False
        assert "empty_pct" in result.observed
        assert "60.0%" in result.observed

    def test_fail_oversized_skills_list(self) -> None:
        # One listing with 101 skills (threshold is 100)
        big = [f"skill_{i}" for i in range(101)]
        facts = self._make_facts([["Python"], big, ["SQL"]])
        result = check_extraction_sanity(facts, BASE_CFG)
        assert result.passed is False
        assert "101" in result.observed
        assert "prose" in result.message.lower() or "list" in result.message.lower()

    def test_pass_exactly_at_pct_boundary(self) -> None:
        # Exactly 20% empty (1 of 5) — should pass (threshold is strictly >)
        facts = self._make_facts([[], ["Python"], ["SQL"], ["Tableau"], ["Excel"]])
        result = check_extraction_sanity(facts, BASE_CFG)
        assert result.passed is True

    def test_pass_empty_facts_list(self) -> None:
        result = check_extraction_sanity([], BASE_CFG)
        assert result.passed is True
        assert "n=0" in result.observed

    def test_pass_skills_at_max_boundary(self) -> None:
        # Exactly 20 skills — should pass (threshold is strictly >)
        exact = [f"skill_{i}" for i in range(20)]
        facts = self._make_facts([exact, ["Python"]])
        result = check_extraction_sanity(facts, BASE_CFG)
        assert result.passed is True


# ---------------------------------------------------------------------------
# check_gap_sample_size
# ---------------------------------------------------------------------------

class TestCheckGapSampleSize:
    def test_pass_top_gap_sufficient_sample(self) -> None:
        gaps = [
            {"skill": "Spark", "sample_size": 10, "rank": 1},
            {"skill": "Scala", "sample_size": 4, "rank": 2},
        ]
        result = check_gap_sample_size(gaps, BASE_CFG)
        assert result.passed is True
        assert "Spark" in result.observed

    def test_fail_top_gap_tiny_sample(self) -> None:
        gaps = [
            {"skill": "Rust", "sample_size": 1, "rank": 1},
            {"skill": "Go", "sample_size": 8, "rank": 2},
        ]
        result = check_gap_sample_size(gaps, BASE_CFG)
        assert result.passed is False
        assert "Rust" in result.observed
        assert "sample_size=1" in result.observed

    def test_pass_empty_gaps(self) -> None:
        result = check_gap_sample_size([], BASE_CFG)
        assert result.passed is True

    def test_pass_pre_sorted_no_rank_key(self) -> None:
        # When no 'rank' key, index 0 is treated as top gap
        gaps = [
            {"skill": "dbt", "sample_size": 5},
            {"skill": "Airflow", "sample_size": 2},
        ]
        result = check_gap_sample_size(gaps, BASE_CFG)
        assert result.passed is True

    def test_fail_pre_sorted_no_rank_key_tiny_sample(self) -> None:
        gaps = [
            {"skill": "Haskell", "sample_size": 2},
            {"skill": "Airflow", "sample_size": 15},
        ]
        result = check_gap_sample_size(gaps, BASE_CFG)
        assert result.passed is False
        assert "Haskell" in result.observed

    def test_pass_exactly_at_min_sample(self) -> None:
        # Exactly 3 — should pass (threshold is strictly <)
        gaps = [{"skill": "Kafka", "sample_size": 3, "rank": 1}]
        result = check_gap_sample_size(gaps, BASE_CFG)
        assert result.passed is True


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------

class TestCheckFreshness:
    def test_pass_fresh_data(self) -> None:
        # 1 day old — well within 3-day threshold
        fetch_at = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)
        result = check_freshness(fetch_at, BASE_CFG, NOW)
        assert result.passed is True
        assert "1.00" in result.observed or "age=" in result.observed

    def test_fail_stale_data(self) -> None:
        # 5 days old — exceeds 3-day threshold
        fetch_at = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
        result = check_freshness(fetch_at, BASE_CFG, NOW)
        assert result.passed is False
        assert "5.00" in result.observed or "age=" in result.observed
        assert "silently failed" in result.message.lower() or "threshold" in result.message.lower()

    def test_fail_none_timestamp(self) -> None:
        result = check_freshness(None, BASE_CFG, NOW)
        assert result.passed is False
        assert "None" in result.observed

    def test_pass_exactly_at_boundary(self) -> None:
        # Exactly 3 days old — should pass (threshold is strictly >)
        fetch_at = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)
        result = check_freshness(fetch_at, BASE_CFG, NOW)
        assert result.passed is True

    def test_fail_one_second_over_boundary(self) -> None:
        # 3 days + 1 second old — just over the threshold
        from datetime import timedelta
        fetch_at = NOW - timedelta(days=3, seconds=1)
        result = check_freshness(fetch_at, BASE_CFG, NOW)
        assert result.passed is False


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

class TestRunAllChecks:
    def _healthy_inputs(self) -> dict:
        return {
            "scores": [20.0, 40.0, 55.0, 70.0, 90.0],
            "facts_list": [
                {"required_skills": ["Python", "SQL"]},
                {"required_skills": ["Tableau"]},
                {"required_skills": ["ETL", "pandas"]},
            ],
            "gaps": [{"skill": "Spark", "sample_size": 5, "rank": 1}],
            "latest_fetch_at": datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc),
            "config": BASE_CFG,
            "now": NOW,
        }

    def test_all_pass(self) -> None:
        verdict = run_all_checks(**self._healthy_inputs())
        assert verdict.passed is True
        assert verdict.failed_checks == []
        assert "All" in verdict.summary

    def test_one_failure_fails_verdict(self) -> None:
        inputs = self._healthy_inputs()
        # Make data stale to trigger check_freshness failure
        inputs["latest_fetch_at"] = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
        verdict = run_all_checks(**inputs)
        assert verdict.passed is False
        assert len(verdict.failed_checks) == 1
        assert verdict.failed_checks[0].name == "check_freshness"
        assert "1/4" in verdict.summary

    def test_multiple_failures_all_collected(self) -> None:
        inputs = self._healthy_inputs()
        # Stale data + too many empty extractions
        inputs["latest_fetch_at"] = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)
        inputs["facts_list"] = [{"required_skills": []} for _ in range(5)]
        verdict = run_all_checks(**inputs)
        assert verdict.passed is False
        assert len(verdict.failed_checks) == 2
        failed_names = {r.name for r in verdict.failed_checks}
        assert "check_freshness" in failed_names
        assert "check_extraction_sanity" in failed_names
        assert "2/4" in verdict.summary
