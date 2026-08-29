"""
edgedash/gaps.py — gap report and trend view.

Usage:
    python -m edgedash.gaps            # latest snapshot as a table
    python -m edgedash.gaps --trend    # compare earliest vs latest snapshot

Both modes are read-only and deterministic. No writes. No extrapolation.

Gap table columns
-----------------
  #     Rank by opportunity cost in this snapshot.
  SKILL Canonical skill name. ⚠ = low confidence (< 3 listings).
  BLOCK listings_blocked — scored listings that require this skill.
  COST  opportunity_cost = Σ(fit_score / 100). Higher → more high-value
        jobs are gated on this skill.
  MEAN  Mean fit score of the blocking listings.
  TOP   Highest fit score among the blocking listings.
  NICE  Listings where this skill appears as nice-to-have only.
  N     Sample size. "?" suffix = low confidence.
  ▐███  Bar proportional to opportunity cost.

Trend table columns
-------------------
  #     Rank in the LATEST snapshot.
  SKILL Canonical skill name.
  EARLIEST  opportunity_cost at the earliest snapshot.
  LATEST    opportunity_cost at the latest snapshot.
  ΔCOST     Absolute change (latest − earliest).
  Δ%        Percent change. "—" when earliest was 0.
  STATUS    NEW (not in earliest top-10), or blank.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import edgedash.storage as storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# Shared formatting
# ---------------------------------------------------------------------------

_BAR_WIDTH = 18
_BAR_CHAR  = "█"
_BAR_EMPTY = "░"
_W         = 92


def _bar(value: float, max_value: float) -> str:
    if max_value <= 0:
        return _BAR_EMPTY * _BAR_WIDTH
    filled = max(0, min(_BAR_WIDTH, round((value / max_value) * _BAR_WIDTH)))
    return _BAR_CHAR * filled + _BAR_EMPTY * (_BAR_WIDTH - filled)


def _fmt_date(iso: str) -> str:
    """Trim an ISO timestamp to 'YYYY-MM-DD HH:MM UTC'."""
    return iso[:16].replace("T", " ") + " UTC"


# ---------------------------------------------------------------------------
# Gap table (default view)
# ---------------------------------------------------------------------------

def _render_table(rows: list[dict]) -> None:
    if not rows:
        print("\n  No gap snapshot found. Run a full cycle first.\n")
        return

    computed_at = _fmt_date(rows[0]["computed_at"])
    max_cost    = max(r["opportunity_cost"] for r in rows)

    print(f"\n{'─' * _W}")
    print(f"  SKILL GAP REPORT   snapshot: {computed_at}")
    print(f"{'─' * _W}")
    print(f"  {'#':>2}  {'SKILL':<22}  {'BLOCK':>5}  {'COST':>6}  "
          f"{'MEAN':>5}  {'TOP':>4}  {'NICE':>4}  {'SAMPLE':<8}  BAR")
    print(f"  {'─'*2}  {'─'*22}  {'─'*5}  {'─'*6}  "
          f"{'─'*5}  {'─'*4}  {'─'*4}  {'─'*8}  {'─'*_BAR_WIDTH}")

    for rank, row in enumerate(rows, start=1):
        label  = row["skill"] + (" ⚠" if row["low_confidence"] else "")
        n_flag = " ?" if row["low_confidence"] else "  "
        sample = f"n={row['listings_blocked']}{n_flag}"
        bar    = _bar(row["opportunity_cost"], max_cost)
        print(
            f"  {rank:>2}  {label:<22}  "
            f"{row['listings_blocked']:>5}  "
            f"{row['opportunity_cost']:>6.1f}  "
            f"{row['mean_score']:>5.1f}  "
            f"{row['top_score']:>4}  "
            f"{row['also_nice_to_have']:>4}  "
            f"{sample:<8}  ▐{bar}▌"
        )

    print(f"{'─' * _W}")
    print(f"  ⚠  = low confidence (< 3 listings)   "
          f"COST = Σ(fit_score/100) for blocked listings")
    print(f"{'─' * _W}\n")


# ---------------------------------------------------------------------------
# Trend view  (--trend)
# ---------------------------------------------------------------------------

def _render_trend() -> None:
    runs = storage.get_all_run_ids()

    if not runs:
        print("\n  No snapshots yet. Run a full cycle first.\n")
        return

    if len(runs) == 1:
        # One data point — honest message, no fabricated trend.
        snap_date = _fmt_date(runs[0]["computed_at"])
        print(f"\n{'─' * _W}")
        print(f"  TREND REPORT — only one snapshot available")
        print(f"{'─' * _W}")
        print(f"  Snapshot date : {snap_date}")
        print(f"  Snapshots     : 1  (need at least 2 to show a trend)")
        print(f"  Days to trend : run the cycle daily — "
              f"you'll have real movement after the next run.")
        print()
        print(f"  No trend is drawn from a single data point.")
        print(f"  Run 'python run_cycle.py' again tomorrow to start")
        print(f"  accumulating history. The snapshot table is append-only,")
        print(f"  so every run is preserved automatically.")
        print(f"{'─' * _W}\n")
        return

    # Two or more snapshots — compare earliest vs latest.
    earliest_run = runs[0]
    latest_run   = runs[-1]

    earliest_rows = storage.get_gap_snapshot_by_run_id(earliest_run["run_id"])
    latest_rows   = storage.get_gap_snapshot_by_run_id(latest_run["run_id"])

    earliest_date = _fmt_date(earliest_run["computed_at"])
    latest_date   = _fmt_date(latest_run["computed_at"])
    n_snapshots   = len(runs)

    # Build lookup: skill → opportunity_cost for the earliest snapshot.
    earliest_by_skill: dict[str, float] = {
        r["skill"]: r["opportunity_cost"] for r in earliest_rows
    }
    earliest_top10: set[str] = {r["skill"] for r in earliest_rows[:10]}

    print(f"\n{'─' * _W}")
    print(f"  TREND REPORT   {n_snapshots} snapshots  "
          f"│  earliest: {earliest_date}  →  latest: {latest_date}")
    print(f"{'─' * _W}")
    print(f"  {'#':>2}  {'SKILL':<22}  {'EARLIEST':>9}  {'LATEST':>7}  "
          f"{'ΔCOST':>7}  {'Δ%':>7}  STATUS")
    print(f"  {'─'*2}  {'─'*22}  {'─'*9}  {'─'*7}  "
          f"{'─'*7}  {'─'*7}  {'─'*8}")

    for rank, row in enumerate(latest_rows[:10], start=1):
        skill = row["skill"]
        latest_cost   = row["opportunity_cost"]
        earliest_cost = earliest_by_skill.get(skill)

        if earliest_cost is None:
            # Skill was not in the earliest snapshot at all.
            delta_abs = "—"
            delta_pct = "—"
            earliest_str = "—"
            status = "NEW"
        else:
            d = latest_cost - earliest_cost
            delta_abs    = f"{d:+.1f}"
            earliest_str = f"{earliest_cost:.1f}"
            if earliest_cost == 0:
                delta_pct = "—"
            else:
                delta_pct = f"{(d / earliest_cost) * 100:+.0f}%"
            status = "" if skill in earliest_top10 else "↑ entered top 10"

        print(
            f"  {rank:>2}  {skill:<22}  "
            f"{earliest_str:>9}  "
            f"{latest_cost:>7.1f}  "
            f"{delta_abs:>7}  "
            f"{delta_pct:>7}  "
            f"{status}"
        )

    # Skills that dropped OUT of the top 10 since the earliest snapshot.
    latest_top10_skills = {r["skill"] for r in latest_rows[:10]}
    dropped = [
        r for r in earliest_rows[:10]
        if r["skill"] not in latest_top10_skills
    ]
    if dropped:
        print()
        print(f"  DROPPED OUT of top 10 since {earliest_date}:")
        for r in dropped:
            print(f"    · {r['skill']:<22}  was {r['opportunity_cost']:.1f}")

    print(f"{'─' * _W}")
    print(f"  ΔCOST = latest − earliest opportunity cost   "
          f"Δ% = percent change   NEW = not in earliest top 10")
    print(f"{'─' * _W}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()
    storage.init_db(cfg.db_path)

    if "--trend" in sys.argv:
        _render_trend()
    else:
        rows = storage.get_latest_gap_snapshot()
        _render_table(rows)


if __name__ == "__main__":
    main()
