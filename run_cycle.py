"""
Entry point — run one full EdgeDash cycle.

Usage
-----
    python run_cycle.py                         # normal run
    python run_cycle.py --dry-run               # plan only, no execution
    python run_cycle.py --force fetcher         # force one agent
    python run_cycle.py --force fetcher --force scorer
    python run_cycle.py --explain               # show state → decision trace
    python run_cycle.py --dry-run --explain     # combine freely

Flags
-----
--dry-run
    Read state, build the plan, print it, and exit without executing
    anything. No writes, no API calls. Exit code 0.

--force <agent>  (repeatable)
    Add the named agent to the plan even if state says skip it.
    Reason is recorded as "forced by operator". A warning is printed
    and the override is written into the cycle summary row.

--explain
    Print the full SystemState with the decision each value drove.
    Useful for debugging "why did it skip that agent?"

Environment variables (APIFY_TOKEN, etc.) are loaded here — this is the
single place in the codebase where .env is read, per steering rule 4.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Load .env before anything else so every module sees the variables.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env")

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from edgedash.config import load_config
from edgedash.orchestrator import run_cycle
import edgedash.storage as storage


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_cycle.py",
        description="Run one full EdgeDash cycle.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without executing anything.",
    )
    parser.add_argument(
        "--force",
        metavar="AGENT",
        action="append",
        default=[],
        dest="forced_agents",
        help=(
            "Force an agent to run even if state says skip. "
            "Repeatable: --force fetcher --force scorer"
        ),
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="Print the full state → decision trace before the plan.",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = _parse_args()
    cfg  = load_config()
    
    # Load user profile from DB to override static config
    storage.init_db(cfg.db_path)
    profile = storage.get_user_profile()
    if profile:
        cfg.override_from_profile(profile)

    run_cycle(
        cfg,
        dry_run=args.dry_run,
        forced_agents=args.forced_agents,
        explain=args.explain,
    )
