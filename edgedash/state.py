"""
edgedash/state.py — cheap system-state snapshot for the Orchestrator.

Public API
----------
read_state(config, now) -> SystemState

Design notes
------------
- `now` is always a parameter; datetime.now() never appears here (testable).
- All reads go through the storage module (steering rule 2).
- Every query is a COUNT or MAX — no full table loads.
- gaps_stale is True when the latest scored_at is newer than the latest
  gap computed_at, meaning the gap snapshot no longer reflects the current
  scores and must be recomputed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from edgedash import storage
from edgedash.config import Config


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SystemState:
    """Immutable snapshot of system state at a point in time.

    Attributes
    ----------
    last_fetch_at:
        ISO-8601 UTC string of the most recent fetched_at, or None if the
        listings table is empty.
    hours_since_fetch:
        Float hours between last_fetch_at and `now`. None when last_fetch_at
        is None (i.e. the database has never been fetched into).
    unscored_count:
        Number of listings where fit_score IS NULL.
    gaps_computed_at:
        ISO-8601 UTC string of the most recent gap snapshot, or None if no
        gap analysis has ever run.
    gaps_stale:
        True if any listing has been scored AFTER the latest gap snapshot,
        meaning the snapshot no longer reflects current scores. Also True
        when gaps_computed_at is None (never computed).
    last_cycle_verdict:
        The `status` field from the most recent cycle_log row, or None.
    last_cycle_at:
        The `started_at` timestamp from the most recent cycle_log row, or None.
    """

    last_fetch_at: str | None
    hours_since_fetch: float | None
    unscored_count: int
    gaps_computed_at: str | None
    gaps_stale: bool
    last_cycle_verdict: str | None
    last_cycle_at: str | None


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def read_state(config: Config, now: datetime) -> SystemState:
    """Read cheap DB queries and return a SystemState snapshot.

    Parameters
    ----------
    config:
        The loaded Config instance (unused for queries today, kept for
        future threshold reads and to match the call signature expected
        by the Orchestrator).
    now:
        The caller-supplied current time. Must be timezone-aware.
        Never call datetime.now() inside this function.

    Returns
    -------
    SystemState
        A frozen snapshot. All arithmetic (hours_since_fetch, gaps_stale)
        is done here so callers and tests see consistent derived values.
    """
    last_fetch_at = storage.last_fetch_time()
    hours_since_fetch = _hours_between(last_fetch_at, now)

    unscored_count = storage.count_unscored()

    gaps_computed_at = storage.last_gap_computed_at()
    last_scored_at = storage.last_scored_at()
    gaps_stale = _compute_gaps_stale(gaps_computed_at, last_scored_at)

    last_verdict, last_cycle_at = _last_cycle_info()

    return SystemState(
        last_fetch_at=last_fetch_at,
        hours_since_fetch=hours_since_fetch,
        unscored_count=unscored_count,
        gaps_computed_at=gaps_computed_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=last_verdict,
        last_cycle_at=last_cycle_at,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_utc(ts: str) -> datetime:
    """Parse an ISO-8601 string to a UTC-aware datetime.

    Handles both "+00:00" suffix and the trailing "Z" form.
    """
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hours_between(ts: str | None, now: datetime) -> float | None:
    """Return fractional hours from `ts` to `now`, or None if `ts` is None."""
    if ts is None:
        return None
    delta = now - _parse_utc(ts)
    return delta.total_seconds() / 3600.0


def _compute_gaps_stale(
    gaps_computed_at: str | None,
    last_scored_at: str | None,
) -> bool:
    """Return True when the gap snapshot is absent or out-of-date.

    Stale conditions:
    - gaps_computed_at is None  → never analysed, must run
    - last_scored_at > gaps_computed_at → new scores exist since last snapshot
    - last_scored_at is None and gaps_computed_at is None → also stale
    """
    if gaps_computed_at is None:
        return True
    if last_scored_at is None:
        # Gaps exist but no scores → snapshot is from a previous db state;
        # treat as not stale (nothing new to incorporate).
        return False
    return _parse_utc(last_scored_at) > _parse_utc(gaps_computed_at)


def _last_cycle_info() -> tuple[str | None, str | None]:
    """Return (status, started_at) from the most recent cycle_log row."""
    rows = storage.get_recent_cycle_log(limit=1)
    if not rows:
        return None, None
    row = rows[0]
    return row.get("status"), row.get("started_at")
