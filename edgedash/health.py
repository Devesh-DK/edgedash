"""
Lightweight health reporting for the EdgeDash system.

Checks:
1. Database unreachable
2. Newest listing older than 3 days
3. No successful cycle in 48 hours
4. Last 3 cycles all failed verification

Exits with code 1 if any check fails, 0 if healthy.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Load .env before anything else
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from edgedash.config import load_config
import edgedash.storage as storage

def main() -> None:
    print("EdgeDash Health Check")
    print("---------------------")
    
    is_healthy = True
    now = datetime.now(timezone.utc)
    
    # Check 1: Database unreachable
    try:
        cfg = load_config()
        storage.init_db(cfg.db_path)
        print("Database connection: OK")
    except Exception as e:
        print(f"Database connection: FAIL ({e})")
        sys.exit(1) # Cannot proceed without DB
        
    # Check 2: Newest listing older than 3 days
    try:
        last_fetch = storage.last_fetch_time()
        if not last_fetch:
            print("Listing freshness: FAIL (No listings found)")
            is_healthy = False
        else:
            last_fetch_dt = datetime.fromisoformat(last_fetch)
            if last_fetch_dt.tzinfo is None:
                last_fetch_dt = last_fetch_dt.replace(tzinfo=timezone.utc)
            days_old = (now - last_fetch_dt).days
            if days_old > 3:
                print(f"Listing freshness: FAIL (Newest listing is {days_old} days old)")
                is_healthy = False
            else:
                print(f"Listing freshness: OK ({days_old} days old)")
    except Exception as e:
        print(f"Listing freshness: ERROR ({e})")
        is_healthy = False

    # Check 3: No successful cycle in 48 hours
    try:
        last_pass = storage.get_last_passing_cycle()
        if not last_pass:
            print("Last successful cycle: FAIL (No passing cycles found)")
            is_healthy = False
        else:
            last_pass_dt = datetime.fromisoformat(last_pass["started_at"])
            if last_pass_dt.tzinfo is None:
                last_pass_dt = last_pass_dt.replace(tzinfo=timezone.utc)
            hours_ago = (now - last_pass_dt).total_seconds() / 3600
            if hours_ago > 48:
                print(f"Last successful cycle: FAIL ({hours_ago:.1f} hours ago)")
                is_healthy = False
            else:
                print(f"Last successful cycle: OK ({hours_ago:.1f} hours ago)")
    except Exception as e:
        print(f"Last successful cycle: ERROR ({e})")
        is_healthy = False

    # Check 4: Last 3 cycles all failed verification
    try:
        recent = storage.get_recent_cycle_log(limit=20)
        orch_runs = [r for r in recent if r["agent"] == "orchestrator"][:3]
        if len(orch_runs) == 3:
            all_failed = True
            for r in orch_runs:
                notes = storage.parse_notes(r.get("notes")) if hasattr(storage, "parse_notes") else {}
                verdict = notes.get("verdict", "unknown")
                if verdict == "pass" or (verdict == "unknown" and notes.get("outcome") == "complete"):
                    all_failed = False
                    break
            if all_failed:
                print("Recent cycles: FAIL (Last 3 orchestrator cycles all failed verification)")
                is_healthy = False
            else:
                print("Recent cycles: OK (At least one pass in last 3 orchestrator runs)")
        else:
            print(f"Recent cycles: OK (Only {len(orch_runs)} orchestrator runs)")
    except Exception as e:
        print(f"Recent cycles: ERROR ({e})")
        is_healthy = False
        
    print("---------------------")
    if is_healthy:
        print("System is HEALTHY.")
        sys.exit(0)
    else:
        print("System is UNHEALTHY.")
        sys.exit(1)

if __name__ == "__main__":
    main()
