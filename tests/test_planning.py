"""
tests/test_planning.py — unit tests for edgedash/planning.py.

build_plan() is a pure function of (SystemState, Config).
No I/O, no DB, no network — every test builds both inline.

Covered scenarios
-----------------
1. everything_stale   — fetch overdue, unscored listings exist, gaps stale
                        → all three agents scheduled to RUN
2. nothing_to_do      — fetch recent, no unscored, gaps fresh
                        → all three agents SKIPPED
3. only_unscored      — fetch recent, unscored > 0, gaps fresh
                        → only scorer runs; fetcher and analyser skipped
4. gaps_stale_no_unscored — fetch recent, unscored = 0, gaps stale
                        → only analyser runs; fetcher and scorer skipped

Each test checks:
  - correct skipped/run assignment per agent
  - reason strings contain the state value that drove the decision (rule 31)
  - stop_conditions come from config, not agent defaults (rule 29)
  - Plan.render() produces a non-empty string without raising
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

import pytest

from edgedash.planning import Plan, Task, build_plan
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Minimal Config factory — only planning-relevant fields
# ---------------------------------------------------------------------------

@dataclass
class _Cfg:
    fetch_interval_hours: int = 6
    fetch_max_pages: int = 5
    fetch_max_listings: int = 200
    scoring_batch_size: int = 25
    score_max_seconds: int = 300
    analyse_max_seconds: int = 120
    max_data_age_days: int = 30


def _cfg(**overrides) -> _Cfg:
    c = _Cfg()
    for k, v in overrides.items():
        object.__setattr__(c, k, v)
    return c


# ---------------------------------------------------------------------------
# SystemState factory helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)
_RECENT   = "2026-08-20T10:00:00+00:00"   # 2 h ago  — within interval
_STALE    = "2026-08-20T05:00:00+00:00"   # 7 h ago  — beyond 6 h interval
_SCORE_TS = "2026-08-20T11:00:00+00:00"   # scored at 11:00
_GAP_OLD  = "2026-08-20T09:00:00+00:00"   # gap snapshot from 09:00 → stale vs 11:00
_GAP_NEW  = "2026-08-20T11:30:00+00:00"   # gap snapshot from 11:30 → fresh vs 11:00


def _state(
    last_fetch_at: str | None = _RECENT,
    hours_since_fetch: float | None = 2.0,
    unscored_count: int = 0,
    gaps_computed_at: str | None = _GAP_NEW,
    gaps_stale: bool = False,
    last_cycle_verdict: str | None = "ok",
    last_cycle_at: str | None = _RECENT,
) -> SystemState:
    return SystemState(
        last_fetch_at=last_fetch_at,
        hours_since_fetch=hours_since_fetch,
        unscored_count=unscored_count,
        gaps_computed_at=gaps_computed_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=last_cycle_verdict,
        last_cycle_at=last_cycle_at,
    )


def _agent(plan: Plan, name: str) -> Task:
    """Return the Task for `name`; fail loudly if absent."""
    for t in plan.tasks:
        if t.agent_name == name:
            return t
    raise AssertionError(f"No task for agent '{name}' in plan")


# ---------------------------------------------------------------------------
# Shared shape assertion
# ---------------------------------------------------------------------------

def _assert_plan_shape(plan: Plan) -> None:
    """Every plan must have exactly four tasks, all with non-empty reasons."""
    assert len(plan.tasks) == 4, f"Expected 4 tasks, got {len(plan.tasks)}"
    for t in plan.tasks:
        assert t.reason, f"Task '{t.agent_name}' has an empty reason"
        assert t.goal,   f"Task '{t.agent_name}' has an empty goal"


# ---------------------------------------------------------------------------
# 1. Everything stale → all three RUN
# ---------------------------------------------------------------------------

def test_everything_stale_all_run() -> None:
    """Fetch overdue + unscored > 0 + gaps stale → all three agents scheduled."""
    state = _state(
        last_fetch_at=_STALE,
        hours_since_fetch=7.0,
        unscored_count=41,
        gaps_computed_at=_GAP_OLD,
        gaps_stale=True,
    )
    plan = build_plan(state, _cfg())

    _assert_plan_shape(plan)

    fetcher  = _agent(plan, "fetcher")
    scorer   = _agent(plan, "scorer")
    analyser = _agent(plan, "analyser")

    assert not fetcher.skipped,  "fetcher should RUN when fetch is overdue"
    assert not scorer.skipped,   "scorer should RUN when unscored_count > 0"
    assert not analyser.skipped, "analyser should RUN when gaps are stale"

    # Reason strings must name the state values (rule 31)
    assert "7.0" in fetcher.reason
    assert "41" in scorer.reason
    assert "gaps_stale=True" in analyser.reason or "gaps_computed_at" in analyser.reason

    # Stop conditions come from config (rule 29)
    assert fetcher.stop_conditions.max_pages == 5
    assert fetcher.stop_conditions.max_items == 200
    assert scorer.stop_conditions.max_items == 25
    assert scorer.stop_conditions.max_seconds == 300
    assert analyser.stop_conditions.max_seconds == 120

    # render() must not raise and must contain agent names
    rendered = plan.render()
    assert "fetcher" in rendered
    assert "scorer" in rendered
    assert "analyser" in rendered


# ---------------------------------------------------------------------------
# 2. Nothing to do → all three SKIPPED
# ---------------------------------------------------------------------------

def test_nothing_to_do_all_skipped() -> None:
    """Fetch recent + no unscored + gaps fresh → all three agents skipped."""
    state = _state(
        last_fetch_at=_RECENT,
        hours_since_fetch=2.0,
        unscored_count=0,
        gaps_computed_at=_GAP_NEW,
        gaps_stale=False,
    )
    plan = build_plan(state, _cfg())

    _assert_plan_shape(plan)

    fetcher  = _agent(plan, "fetcher")
    scorer   = _agent(plan, "scorer")
    analyser = _agent(plan, "analyser")

    assert fetcher.skipped,  "fetcher should be SKIPPED when fetch is recent"
    assert scorer.skipped,   "scorer should be SKIPPED when unscored_count=0"
    assert analyser.skipped, "analyser should be SKIPPED when gaps are fresh"

    # Reasons must still name the deciding state value
    assert "2.0" in fetcher.reason
    assert "unscored_count=0" in scorer.reason
    assert "gaps_stale=False" in analyser.reason

    # fetcher, scorer, analyser skipped, but janitor runs unconditionally
    assert len(plan.runnable()) == 1
    assert plan.runnable()[0].agent_name == "janitor"
    assert len(plan.skipped_tasks()) == 3

    # render() includes [SKIP] markers for three, [RUN] for janitor
    rendered = plan.render()
    assert rendered.count("[SKIP]") == 3
    assert rendered.count("[RUN]") == 1


# ---------------------------------------------------------------------------
# 3. Only unscored listings → scorer runs; fetcher and analyser skipped
# ---------------------------------------------------------------------------

def test_only_unscored_scorer_runs() -> None:
    """Fetch recent + unscored > 0 + gaps fresh → only scorer runs."""
    state = _state(
        last_fetch_at=_RECENT,
        hours_since_fetch=2.0,
        unscored_count=15,
        gaps_computed_at=_GAP_NEW,
        gaps_stale=False,
    )
    plan = build_plan(state, _cfg())

    _assert_plan_shape(plan)

    fetcher  = _agent(plan, "fetcher")
    scorer   = _agent(plan, "scorer")
    analyser = _agent(plan, "analyser")

    assert fetcher.skipped,   "fetcher should be SKIPPED"
    assert not scorer.skipped, "scorer should RUN when unscored_count > 0"
    assert analyser.skipped,  "analyser should be SKIPPED when gaps are fresh"

    assert "15" in scorer.reason

    runnable = plan.runnable()
    assert len(runnable) == 2
    assert any(r.agent_name == "scorer" for r in runnable)
    assert any(r.agent_name == "janitor" for r in runnable)

    rendered = plan.render()
    assert rendered.count("[RUN]") == 2
    assert rendered.count("[SKIP]") == 2


# ---------------------------------------------------------------------------
# 4. Gaps stale but nothing unscored → only analyser runs
# ---------------------------------------------------------------------------

def test_gaps_stale_no_unscored_analyser_runs() -> None:
    """Fetch recent + unscored = 0 + gaps stale → only analyser runs."""
    state = _state(
        last_fetch_at=_RECENT,
        hours_since_fetch=2.0,
        unscored_count=0,
        gaps_computed_at=_GAP_OLD,
        gaps_stale=True,
    )
    plan = build_plan(state, _cfg())

    _assert_plan_shape(plan)

    fetcher  = _agent(plan, "fetcher")
    scorer   = _agent(plan, "scorer")
    analyser = _agent(plan, "analyser")

    assert fetcher.skipped,    "fetcher should be SKIPPED"
    assert scorer.skipped,     "scorer should be SKIPPED when unscored_count=0"
    assert not analyser.skipped, "analyser should RUN when gaps are stale"

    # Reason must reference the staleness signal
    assert "gaps_stale" in analyser.reason or "gaps_computed_at" in analyser.reason

    runnable = plan.runnable()
    assert len(runnable) == 2
    assert any(r.agent_name == "analyser" for r in runnable)
    assert any(r.agent_name == "janitor" for r in runnable)

    rendered = plan.render()
    assert rendered.count("[RUN]") == 2
    assert rendered.count("[SKIP]") == 2


# ---------------------------------------------------------------------------
# Extra: never-fetched state (hours_since_fetch is None)
# ---------------------------------------------------------------------------

def test_never_fetched_fetch_runs() -> None:
    """hours_since_fetch=None means the DB is empty → fetcher must run."""
    state = _state(
        last_fetch_at=None,
        hours_since_fetch=None,
        unscored_count=0,
        gaps_computed_at=None,
        gaps_stale=True,
    )
    plan = build_plan(state, _cfg())

    fetcher  = _agent(plan, "fetcher")
    analyser = _agent(plan, "analyser")

    assert not fetcher.skipped, "fetcher must RUN when never fetched"
    assert "None" in fetcher.reason

    # Analyser also runs because gaps_computed_at is None
    assert not analyser.skipped
    assert "None" in analyser.reason


# ---------------------------------------------------------------------------
# Extra: config thresholds are respected
# ---------------------------------------------------------------------------

def test_custom_thresholds_respected() -> None:
    """A custom fetch_interval_hours of 12 keeps a 7-hour-old fetch as fresh."""
    state = _state(
        last_fetch_at=_STALE,
        hours_since_fetch=7.0,   # 7 h < 12 h threshold
        unscored_count=0,
        gaps_computed_at=_GAP_NEW,
        gaps_stale=False,
    )
    plan = build_plan(state, _cfg(fetch_interval_hours=12))

    fetcher = _agent(plan, "fetcher")
    assert fetcher.skipped, (
        "fetcher should be SKIPPED when hours_since_fetch < custom threshold"
    )
    assert "12" in fetcher.reason
