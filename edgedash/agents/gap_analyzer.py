"""
edgedash/agents/gap_analyzer.py — GapAnalyzer agent.

Deterministic only. No LLM call anywhere in this file.

For every scored listing that has extracted facts, compares the listing's
required_skills against the user's canonical skill set and accumulates gap
statistics. Results are written as a timestamped snapshot (steering rule 25)
so every run is preserved and trend-over-time is possible.

stop_conditions respected (rule 29):
- max_seconds : wall-clock budget; analysis exits cleanly when exceeded
  (snapshot is still written for however many gaps were computed).

Opportunity-cost arithmetic (steering rule 24)
----------------------------------------------
    opportunity_cost(skill) = Σ (listing.fit_score / 100)
                               for every scored listing that requires
                               this skill and where the user lacks it.

    A listing scored 85 contributes 0.85.
    A listing scored 20 contributes 0.20.
    Ranking by this sum means a single high-value listing outweighs
    many low-value ones — gaps that cost me real opportunities surface first.

AgentResult.notes format:
    "10 gaps · top: kubernetes (31 listings, cost 24.1) · 58 listings analysed"
"""

from __future__ import annotations

import hashlib
import logging
import statistics
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.skills import canonical

logger = logging.getLogger(__name__)

# Gaps with fewer than this many listings are flagged low-confidence (rule 27).
_LOW_CONFIDENCE_THRESHOLD = 3

# Maximum example listing IDs stored per gap (rule 26 — must be traceable).
_MAX_EXAMPLE_IDS = 5

# Top-N gaps to persist and report in the notes line.
_TOP_N = 10


class GapAnalyzer:
    name: str = "GapAnalyzer"

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        started_at = storage.now_utc()
        wall_start = time.monotonic()

        max_seconds: float | None = (
            float(stop_conditions.max_seconds)
            if stop_conditions.max_seconds is not None
            else None
        )

        # ── 1. Load scored listings with extracted facts ───────────────────
        listings = storage.get_scored_listings_with_facts()
        if not listings:
            notes = "no scored listings with facts — nothing to analyse"
            storage.log_cycle(
                agent=self.name,
                started_at=started_at,
                finished_at=storage.now_utc(),
                records_touched=0,
                status="ok",
                notes=notes,
            )
            return AgentResult(
                agent=self.name, status="ok", records_touched=0, notes=notes,
            )

        # ── 2. Build canonical user skill set ─────────────────────────────
        aliases = config.skill_aliases
        my_skills: set[str] = {
            canonical(s, aliases) for s in config.my_skills if s.strip()
        }

        # ── 3. Accumulate gap statistics ───────────────────────────────────
        # Per-skill accumulators — each value is a list of (fit_score, listing_id)
        required_gaps: dict[str, list[tuple[int, str]]] = defaultdict(list)
        nice_counts:   dict[str, int]                   = defaultdict(int)
        budget_exceeded = False

        for listing in listings:
            if max_seconds is not None:
                if time.monotonic() - wall_start >= max_seconds:
                    logger.warning(
                        "GapAnalyzer: max_seconds=%s reached after %d listings — stopping",
                        max_seconds, listings.index(listing),
                    )
                    budget_exceeded = True
                    break

            score: int = listing["fit_score"]
            lid:   str = listing["id"]
            facts: dict[str, Any] = listing["facts"]

            required: list[str] = facts.get("required_skills") or []
            nice:     list[str] = facts.get("nice_to_have") or []

            for raw_skill in required:
                canon = canonical(raw_skill, aliases)
                if not canon:
                    continue
                if canon not in my_skills:
                    required_gaps[canon].append((score, lid))

            for raw_skill in nice:
                canon = canonical(raw_skill, aliases)
                if canon and canon not in my_skills:
                    nice_counts[canon] += 1

        # ── 4. Compute per-skill metrics ───────────────────────────────────
        gap_rows: list[dict] = []

        for skill, occurrences in required_gaps.items():
            scores_list = [s for s, _ in occurrences]
            ids_by_score = [lid for _, lid in sorted(occurrences, reverse=True)]

            listings_blocked = len(occurrences)
            opportunity_cost = sum(s / 100.0 for s in scores_list)
            mean_score       = statistics.mean(scores_list)
            top_score        = max(scores_list)
            example_ids      = ids_by_score[:_MAX_EXAMPLE_IDS]
            low_confidence   = listings_blocked < _LOW_CONFIDENCE_THRESHOLD

            gap_rows.append({
                "skill":             skill,
                "listings_blocked":  listings_blocked,
                "opportunity_cost":  round(opportunity_cost, 3),
                "mean_score":        round(mean_score, 1),
                "top_score":         top_score,
                "example_ids":       example_ids,
                "also_nice_to_have": nice_counts.get(skill, 0),
                "low_confidence":    low_confidence,
            })

        # ── 5. Rank by opportunity_cost descending (rule 24) ───────────────
        gap_rows.sort(key=lambda r: r["opportunity_cost"], reverse=True)
        top_gaps = gap_rows[:_TOP_N]

        # ── 6. Write timestamped snapshot (rule 25 — never overwrite) ─────
        computed_at = storage.now_utc()
        run_id      = _make_run_id(computed_at)

        storage.save_gap_snapshot(run_id, computed_at, top_gaps)

        logger.info(
            "GapAnalyzer: %d gaps found across %d listings, top=%s",
            len(gap_rows),
            len(listings),
            top_gaps[0]["skill"] if top_gaps else "none",
        )

        # ── 7. Log cycle and return ────────────────────────────────────────
        notes = _build_notes(top_gaps, len(listings), budget_exceeded)

        storage.log_cycle(
            agent=self.name,
            started_at=started_at,
            finished_at=storage.now_utc(),
            records_touched=len(top_gaps),
            status="ok",
            notes=notes,
        )

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(top_gaps),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run_id(computed_at: str) -> str:
    """Generate a short, stable run ID from the timestamp."""
    return hashlib.sha256(computed_at.encode()).hexdigest()[:12]


def _build_notes(
    top_gaps: list[dict], listings_analysed: int, budget_exceeded: bool
) -> str:
    """Compose the AgentResult notes string from computed gap data."""
    suffix = " · budget exceeded" if budget_exceeded else ""

    if not top_gaps:
        return f"0 gaps · {listings_analysed} listings analysed{suffix}"

    top = top_gaps[0]
    top_str = (
        f"{top['skill']} "
        f"({top['listings_blocked']} listings, "
        f"cost {top['opportunity_cost']:.1f})"
    )
    return (
        f"{len(top_gaps)} gaps · "
        f"top: {top_str} · "
        f"{listings_analysed} listings analysed{suffix}"
    )
