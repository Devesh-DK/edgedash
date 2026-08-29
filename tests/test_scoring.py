"""
tests/test_scoring.py — unit tests for edgedash/scoring.py.

score_listing() is a pure function: no I/O, no DB, no network.
Every test builds a minimal Config + facts + listing and asserts on the
returned dict's keys and the arithmetic ranges.

Covered cases (as specified):
  1. perfect_match       — all required skills present, exact seniority, remote, fresh
  2. zero_match          — no required skills present, wrong seniority, on-site elsewhere
  3. empty_required_skills — facts has no required skills (division-by-zero guard)
  4. null_posted_at      — posted_at is None (recency must return 0.5, not crash)
  5. null_remote_ok      — remote_ok is None and location unknown (location_fit -> 0.5)
  6. seniority_three_bands_off — junior vs lead (distance=3, seniority_fit -> 0.0)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List

import pytest

from edgedash.scoring import score_listing, build_reason, _RECENCY_DECAY_DAYS


# ---------------------------------------------------------------------------
# Minimal Config factory — only fields used by scoring.py
# ---------------------------------------------------------------------------

def _make_config(
    my_skills: List[str] | None = None,
    target_seniority: str = "mid",
    target_city: str = "Remote",
    weight_skill_match: float = 0.45,
    weight_seniority_fit: float = 0.25,
    weight_location_fit: float = 0.15,
    weight_recency: float = 0.15,
) -> object:
    """Return a plain namespace that satisfies scoring.py's Config usage."""

    @dataclass
    class _Cfg:
        my_skills: List[str]
        target_seniority: str
        target_city: str
        weight_skill_match: float
        weight_seniority_fit: float
        weight_location_fit: float
        weight_recency: float

    return _Cfg(
        my_skills=my_skills or [],
        target_seniority=target_seniority,
        target_city=target_city,
        weight_skill_match=weight_skill_match,
        weight_seniority_fit=weight_seniority_fit,
        weight_location_fit=weight_location_fit,
        weight_recency=weight_recency,
    )


def _today_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Shared assertions
# ---------------------------------------------------------------------------

def _assert_result_shape(result: dict) -> None:
    """Every result must have the right keys with sane types."""
    assert isinstance(result["score"], int)
    assert 0 <= result["score"] <= 100
    assert isinstance(result["reason"], str)
    assert len(result["reason"]) > 0
    comps = result["components"]
    for key in ("skill_match", "seniority_fit", "location_fit", "recency"):
        assert key in comps
        assert 0.0 <= comps[key] <= 1.0, f"{key} out of range: {comps[key]}"


# ---------------------------------------------------------------------------
# 1. Perfect match
# ---------------------------------------------------------------------------

def test_perfect_match() -> None:
    """All required skills present, exact seniority, remote OK, posted today.

    Expected: score is high (well above 80), all four components near 1.0.
    """
    config = _make_config(
        my_skills=["python", "sql", "pandas"],
        target_seniority="mid",
    )
    facts = {
        "required_skills": ["python", "sql", "pandas"],
        "nice_to_have": [],
        "seniority": "mid",
        "remote_ok": True,
        "years_required": None,
    }
    listing = {"location": None, "posted_at": _today_iso()}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["skill_match"] == pytest.approx(1.0)
    assert result["components"]["seniority_fit"] == pytest.approx(1.0)
    assert result["components"]["location_fit"] == pytest.approx(1.0)
    assert result["components"]["recency"] == pytest.approx(1.0, abs=0.05)
    assert result["score"] >= 95, f"Expected near-100 score, got {result['score']}"

    # Reason must mention the skill count and no gap
    assert "3/3" in result["reason"]
    assert "gap" not in result["reason"]


# ---------------------------------------------------------------------------
# 2. Zero match
# ---------------------------------------------------------------------------

def test_zero_match() -> None:
    """Candidate has none of the required skills, wrong seniority, on-site elsewhere.

    Expected: score is low (well below 30), skill_match near 0, seniority_fit 0.
    """
    config = _make_config(
        my_skills=["excel"],
        target_seniority="junior",
    )
    facts = {
        "required_skills": ["kubernetes", "rust", "go"],
        "nice_to_have": ["terraform"],
        "seniority": "lead",       # three bands above junior -> 0.0
        "remote_ok": False,
        "years_required": 8,
    }
    listing = {"location": "San Francisco", "posted_at": _days_ago_iso(20)}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["skill_match"] == pytest.approx(0.0, abs=0.01)
    assert result["components"]["seniority_fit"] == pytest.approx(0.0)
    assert result["components"]["location_fit"] == pytest.approx(0.1)
    assert result["score"] <= 20, f"Expected low score, got {result['score']}"

    # All missing skills must appear in the reason
    assert "gap" in result["reason"]
    assert "kubernetes" in result["reason"]


# ---------------------------------------------------------------------------
# 3. Empty required_skills (division-by-zero guard)
# ---------------------------------------------------------------------------

def test_empty_required_skills() -> None:
    """facts.required_skills is empty — must not divide by zero.

    skill_match must be 1.0 (no requirements = no gaps = full marks).
    """
    config = _make_config(my_skills=["python"])
    facts = {
        "required_skills": [],
        "nice_to_have": [],
        "seniority": "mid",
        "remote_ok": True,
        "years_required": None,
    }
    listing = {"location": None, "posted_at": _today_iso()}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["skill_match"] == pytest.approx(1.0), (
        "Empty required_skills should yield skill_match=1.0"
    )
    assert "no required skills listed" in result["reason"]
    assert "gap" not in result["reason"]


# ---------------------------------------------------------------------------
# 4. Null posted_at
# ---------------------------------------------------------------------------

def test_null_posted_at() -> None:
    """posted_at is None — recency must return 0.5 and not raise."""
    config = _make_config(my_skills=["python"], target_seniority="mid")
    facts = {
        "required_skills": ["python"],
        "nice_to_have": [],
        "seniority": "mid",
        "remote_ok": True,
        "years_required": None,
    }
    listing = {"location": None, "posted_at": None}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["recency"] == pytest.approx(0.5), (
        "None posted_at must yield recency=0.5 (neutral)"
    )
    assert "posted date unknown" in result["reason"]


# ---------------------------------------------------------------------------
# 5. Null remote_ok + unknown location
# ---------------------------------------------------------------------------

def test_null_remote_ok_unknown_location() -> None:
    """remote_ok is None, location is None/empty — location_fit must be 0.5."""
    config = _make_config(
        my_skills=["python"],
        target_seniority="mid",
        target_city="Bengaluru",
    )
    facts = {
        "required_skills": ["python"],
        "nice_to_have": [],
        "seniority": "mid",
        "remote_ok": None,    # not stated
        "years_required": None,
    }
    listing = {"location": None, "posted_at": _today_iso()}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["location_fit"] == pytest.approx(0.5), (
        "remote_ok=None + no location must yield location_fit=0.5"
    )
    assert "location unknown" in result["reason"]


# ---------------------------------------------------------------------------
# 6. Seniority three bands off (junior vs lead)
# ---------------------------------------------------------------------------

def test_seniority_three_bands_off() -> None:
    """Distance between junior (0) and lead (3) is 3 — seniority_fit must be 0.0."""
    config = _make_config(
        my_skills=["python"],
        target_seniority="junior",
    )
    facts = {
        "required_skills": ["python"],
        "nice_to_have": [],
        "seniority": "lead",
        "remote_ok": True,
        "years_required": 10,
    }
    listing = {"location": None, "posted_at": _today_iso()}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["seniority_fit"] == pytest.approx(0.0), (
        "junior vs lead (3 bands apart) must yield seniority_fit=0.0"
    )
    assert "mismatch" in result["reason"]


# ---------------------------------------------------------------------------
# Extra: recency decay boundary — 30-day-old listing scores 0.0
# ---------------------------------------------------------------------------

def test_recency_at_decay_boundary() -> None:
    """A listing posted exactly 30 days ago should have recency ≈ 0.0."""
    config = _make_config(my_skills=[])
    facts = {
        "required_skills": [],
        "nice_to_have": [],
        "seniority": "unknown",
        "remote_ok": None,
        "years_required": None,
    }
    listing = {"location": None, "posted_at": _days_ago_iso(_RECENCY_DECAY_DAYS)}

    result = score_listing(listing, facts, config)
    _assert_result_shape(result)

    assert result["components"]["recency"] == pytest.approx(0.0, abs=0.02)
