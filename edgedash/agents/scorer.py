"""
edgedash/agents/scorer.py — Scorer agent.

Selects unscored listings from storage, extracts facts (via the cached
extractor), scores them with the deterministic scorer, writes results back,
and logs the score distribution every run (steering rules 18, 20, 21).

Per-listing failures are isolated: one bad listing is logged and skipped;
the rest of the batch continues (steering rule 17).

stop_conditions respected (rule 29):
- max_items   : overrides config.scoring_batch_size when present.
- max_seconds : wall-clock budget; scoring loop exits cleanly when exceeded.

AgentResult.notes format:
    "scored 25 · range 31-89 · mean 58 · 2 failed · spread OK"
    "scored 4 · range 55-61 · mean 58 · 0 failed · SUSPECT (spread 6)"
"""

from __future__ import annotations

import logging
import statistics
import time
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.scoring import score_listing

logger = logging.getLogger(__name__)


class Scorer:
    name: str = "Scorer"

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        started_at = storage.now_utc()
        wall_start = time.monotonic()

        # ── 1. Resolve limits from stop_conditions, fall back to config ────
        batch_size = (
            stop_conditions.max_items
            if stop_conditions.max_items is not None
            else config.scoring_batch_size
        )
        max_seconds: float | None = (
            float(stop_conditions.max_seconds)
            if stop_conditions.max_seconds is not None
            else None
        )

        # ── 2. Select batch (rules 18, 21) ─────────────────────────────────
        batch = storage.get_unscored_listings(batch_size)
        if not batch:
            notes = "no unscored listings — nothing to do"
            storage.log_cycle(
                agent=self.name,
                started_at=started_at,
                finished_at=storage.now_utc(),
                records_touched=0,
                status="ok",
                notes=notes,
            )
            return AgentResult(
                agent=self.name, status="ok", records_touched=0, notes=notes
            )

        # ── 3. Score each listing ───────────────────────────────────────────
        scores: list[int] = []
        failed = 0
        budget_exceeded = False

        for listing in batch:
            # Wall-clock budget check
            if max_seconds is not None:
                elapsed = time.monotonic() - wall_start
                if elapsed >= max_seconds:
                    logger.warning(
                        "Scorer: max_seconds=%s reached after %d scored — stopping",
                        max_seconds, len(scores),
                    )
                    budget_exceeded = True
                    break

            listing_id: str = listing.get("id", "<unknown>")
            try:
                facts: dict[str, Any] = extract(listing, config)
                result = score_listing(listing, facts, config)

                storage.save_score(
                    listing_id=listing_id,
                    score=result["score"],
                    reason=result["reason"],
                    components=result["components"],
                    scored_at=storage.now_utc(),
                )
                scores.append(result["score"])
                logger.debug(
                    "Scorer: listing %s scored %d — %s",
                    listing_id, result["score"], result["reason"],
                )

            except Exception as exc:  # noqa: BLE001
                failed += 1
                msg = f"listing {listing_id} failed: {type(exc).__name__}: {exc}"
                logger.error("Scorer: %s", msg)
                storage.log_cycle(
                    agent=self.name,
                    started_at=started_at,
                    finished_at=storage.now_utc(),
                    records_touched=0,
                    status="failed",
                    notes=msg[:500],
                )

        # ── 4. Distribution + suspect check (rule 20) ──────────────────────
        notes = _build_notes(scores, failed, budget_exceeded)
        dist_status = _distribution_status(scores)
        final_status: str = "suspect" if dist_status == "suspect" else "ok"

        logger.info("Scorer: %s", notes)

        storage.log_cycle(
            agent=self.name,
            started_at=started_at,
            finished_at=storage.now_utc(),
            records_touched=len(scores),
            status=final_status,
            notes=notes,
        )

        return AgentResult(
            agent=self.name,
            status="ok",  # suspect is an internal flag; the agent didn't fail
            records_touched=len(scores),
            notes=notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _distribution_status(scores: list[int]) -> str:
    """Return "ok" or "suspect" based on spread (rule 20).

    Spread < 10 across any batch larger than one listing is flagged suspect.
    A single-listing batch always returns "ok" — spread is meaningless.
    """
    if len(scores) < 2:
        return "ok"
    spread = max(scores) - min(scores)
    return "suspect" if spread < 10 else "ok"


def _build_notes(scores: list[int], failed: int, budget_exceeded: bool) -> str:
    """Compose the AgentResult notes string from distribution statistics."""
    fail_str = f"{failed} failed"
    suffix = " · budget exceeded" if budget_exceeded else ""

    if not scores:
        return f"scored 0 · {fail_str}{suffix}"

    lo = min(scores)
    hi = max(scores)
    mean = round(statistics.mean(scores))
    spread = hi - lo
    dist_str = _distribution_status(scores)
    spread_label = f"SUSPECT (spread {spread})" if dist_str == "suspect" else "spread OK"

    return (
        f"scored {len(scores)} · range {lo}-{hi} · mean {mean} "
        f"· {fail_str} · {spread_label}{suffix}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# python -m edgedash.agents.scorer [--limit N]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import dataclasses
    import logging
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s  %(name)s  %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Run the EdgeDash Scorer against unscored listings."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Override scoring_batch_size for this run.",
    )
    args = parser.parse_args()

    from edgedash.config import load_config
    from edgedash.planning import StopConditions

    cfg = load_config()
    storage.init_db(cfg.db_path)
    sc = StopConditions(
        max_items=args.limit if args.limit is not None else cfg.scoring_batch_size
    )
    result = Scorer().run(cfg, sc)
    print(result)
