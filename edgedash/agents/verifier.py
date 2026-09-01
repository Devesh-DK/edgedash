"""
edgedash/agents/verifier.py — Verifier agent (rules 34-39).

Reads the current cycle's data from storage, runs every plausibility check,
and returns a verdict. That is ALL it does.

Rule 34: The Verifier NEVER repairs, rewrites, or adjusts data.
         It returns a verdict and a reason. The Orchestrator decides what
         to do about a failure.

Rule 35: Checks test properties of the output distribution and shape,
         not the accuracy of any single value.

No LLM anywhere in this file.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.verification import Verdict, run_all_checks

logger = logging.getLogger(__name__)


class Verifier:
    name: str = "Verifier"

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        """Read current cycle data from storage and run all plausibility checks.

        Writes nothing except its own cycle_log row (rule 34).
        Returns status="failed" when any check fails so the Orchestrator
        can decide whether to retry or degrade (rule 36).
        """
        started_at = storage.now_utc()
        now = datetime.now(timezone.utc)

        # ── 1. Gather data from storage ────────────────────────────────────
        scores, facts_list, gaps, latest_fetch_at = _load_inputs()

        # ── 2. Run all plausibility checks ─────────────────────────────────
        verdict: Verdict = run_all_checks(
            scores=scores,
            facts_list=facts_list,
            gaps=gaps,
            latest_fetch_at=latest_fetch_at,
            config=config,
            now=now,
        )

        # ── 3. Build a log line for every failed check (rule 37) ──────────
        #       Never just "failed" — always name the check and observed value.
        if verdict.passed:
            log_line = f"VERDICT: pass — {verdict.summary}"
            agent_status = "ok"
            logger.info("Verifier: %s", log_line)
        else:
            detail = "  |  ".join(
                f"{r.name} observed {r.observed} (threshold {r.threshold})"
                for r in verdict.failed_checks
            )
            log_line = f"VERDICT: fail — {detail}"
            agent_status = "failed"
            logger.warning("Verifier: %s", log_line)

        # ── 4. Serialize verdict into notes for cycle_log (rule 37) ────────
        notes_payload: dict[str, Any] = {
            "verdict": "pass" if verdict.passed else "fail",
            "summary": verdict.summary,
            "failed_checks": [
                {
                    "name":      r.name,
                    "observed":  r.observed,
                    "threshold": r.threshold,
                    "message":   r.message,
                }
                for r in verdict.failed_checks
            ],
        }

        storage.log_cycle(
            agent=self.name,
            started_at=started_at,
            finished_at=storage.now_utc(),
            records_touched=0,           # Verifier writes no data rows
            status=agent_status,
            notes=json.dumps(notes_payload),
        )

        # ── 5. Return verdict in notes so Orchestrator can inspect it ──────
        return AgentResult(
            agent=self.name,
            status=agent_status,
            records_touched=0,
            notes=log_line,
        )


# ---------------------------------------------------------------------------
# Storage input loader — isolated so it can be replaced or extended easily
# ---------------------------------------------------------------------------

def _load_inputs() -> tuple[
    list[float],                # scores
    list[dict],                 # facts_list
    list[dict],                 # gaps
    datetime | None,            # latest_fetch_at
]:
    """Pull the four data slices the checks need from storage.

    Scores come from all currently scored listings.
    Facts come from the extraction cache (joined via description_hash).
    Gaps come from the latest gap snapshot.
    Fetch timestamp is the most recent fetched_at across all listings.
    """
    # Scores: from every scored listing
    scored_with_facts = storage.get_scored_listings_with_facts()
    scores: list[float] = [float(row["fit_score"]) for row in scored_with_facts]

    # Facts: required_skills from each listing's extraction
    facts_list: list[dict] = [
        row.get("facts") or {} for row in scored_with_facts
    ]

    # Gaps: latest snapshot (check_gap_sample_size reads sample_size + rank)
    raw_gaps = storage.get_latest_gap_snapshot()
    gaps: list[dict] = [
        {
            "skill":       g["skill"],
            "sample_size": g["listings_blocked"],   # mapped to check's expected key
            "rank":        i,                        # index = rank (0 = top)
        }
        for i, g in enumerate(raw_gaps)             # already sorted by opportunity_cost
    ]

    # Latest fetch time
    raw_ts = storage.last_fetch_time()
    latest_fetch_at: datetime | None = None
    if raw_ts:
        try:
            latest_fetch_at = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            # Ensure timezone-aware for arithmetic in check_freshness
            if latest_fetch_at.tzinfo is None:
                latest_fetch_at = latest_fetch_at.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Verifier: could not parse last_fetch_time %r", raw_ts)

    return scores, facts_list, gaps, latest_fetch_at
