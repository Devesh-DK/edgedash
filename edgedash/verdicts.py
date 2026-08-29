"""
edgedash/verdicts.py -- Read-only verification history viewer.

Usage
-----
    python -m edgedash.verdicts
    python -m edgedash.verdicts --check check_freshness

Read-only. Queries cycle_log through the storage module only (rule 2).
No writes. No schema changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Load .env before storage so any env-dependent config is available.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import edgedash.storage as storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# Terminal color helpers (no third-party deps -- raw ANSI)
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()

_RESET  = "\033[0m"  if _USE_COLOR else ""
_BOLD   = "\033[1m"  if _USE_COLOR else ""
_DIM    = "\033[2m"  if _USE_COLOR else ""
_GREEN  = "\033[32m" if _USE_COLOR else ""
_YELLOW = "\033[33m" if _USE_COLOR else ""
_RED    = "\033[31m" if _USE_COLOR else ""
_CYAN   = "\033[36m" if _USE_COLOR else ""
_WHITE  = "\033[37m" if _USE_COLOR else ""


def _green(s: str)  -> str: return f"{_GREEN}{s}{_RESET}"
def _yellow(s: str) -> str: return f"{_YELLOW}{s}{_RESET}"
def _red(s: str)    -> str: return f"{_RED}{s}{_RESET}"
def _cyan(s: str)   -> str: return f"{_CYAN}{s}{_RESET}"
def _bold(s: str)   -> str: return f"{_BOLD}{s}{_RESET}"
def _dim(s: str)    -> str: return f"{_DIM}{s}{_RESET}"


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def _parse_notes(raw) -> dict:
    """Safely parse a notes field that may be a str or already a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _fmt_ts(ts: str | None) -> str:
    """ISO timestamp -> compact readable form."""
    if not ts:
        return "never"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, AttributeError):
        return str(ts)


def _verdict_of(notes: dict, status: str) -> str:
    """Derive a verdict string from notes, falling back to cycle status."""
    v = notes.get("verdict")
    if v in ("pass", "fail", "degraded"):
        return v
    # Pre-verifier cycles: treat complete as pass, anything else as unknown
    outcome = notes.get("outcome", status)
    if outcome == "complete":
        return "pass"
    if outcome in ("partial", "degraded"):
        return "degraded"
    return "unknown"


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _build_rows(raw_cycles: list[dict]) -> list[dict]:
    """Parse raw cycle_log rows into display-ready dicts.

    The orchestrator summary row carries verdict/retry_count/outcome but
    not failed_checks -- those live in the Verifier's own cycle_log row.
    We join them by matching each orchestrator row to the most recent
    Verifier row whose started_at is <= the orchestrator's started_at.
    raw_cycles is already sorted newest-first.
    """
    # Separate orchestrator and verifier rows (newest-first)
    orch_rows     = [r for r in raw_cycles if r.get("agent") == "orchestrator"]
    verifier_rows = [r for r in raw_cycles if r.get("agent") == "Verifier"]

    # Build a lookup of verifier notes indexed by started_at string for fast access
    # verifier_rows is newest-first, so we scan forward to find the one that
    # fired just before each orchestrator summary.
    rows = []
    for c in orch_rows:
        notes   = _parse_notes(c.get("notes"))
        verdict = _verdict_of(notes, c.get("status", ""))
        ran     = notes.get("ran") or []
        retries = int(notes.get("retry_count", 0))
        elapsed = float(notes.get("elapsed_s", 0.0))

        # Find the most recent Verifier row that ran during this cycle.
        # Match only when the Verifier's started_at falls between the
        # orchestrator's started_at and finished_at (inclusive).
        # Pre-Verifier cycles have no Verifier row -- leave failed_checks empty.
        orch_ts     = c.get("started_at", "")
        orch_fin_ts = c.get("finished_at") or ""
        failed_checks: list[dict] = []

        if orch_fin_ts:
            for vr in verifier_rows:
                vr_ts = vr.get("started_at", "")
                if orch_ts <= vr_ts <= orch_fin_ts:
                    vr_notes = _parse_notes(vr.get("notes"))
                    failed_checks = vr_notes.get("failed_checks") or []
                    break

        rows.append({
            "ts":            c.get("started_at", ""),
            "ran":           ran,
            "verdict":       verdict,
            "failed_checks": failed_checks,
            "retries":       retries,
            "elapsed_s":     elapsed,
            "status":        c.get("status", ""),
        })
    return rows


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_W = 72  # console width


def _verdict_tag(verdict: str) -> str:
    """Colored, fixed-width verdict tag."""
    tags = {
        "pass":     _green( "PASS    "),
        "fail":     _yellow("FAIL    "),
        "degraded": _red(   "DEGRADED"),
        "unknown":  _dim(   "UNKNOWN "),
    }
    return _bold(tags.get(verdict, _dim(verdict.upper().ljust(8))))


def _render_row(row: dict) -> None:
    """Print one cycle row to stdout."""
    ts      = _fmt_ts(row["ts"])
    verdict = row["verdict"]
    ran     = ", ".join(row["ran"]) if row["ran"] else _dim("--")
    retries = row["retries"]
    elapsed = row["elapsed_s"]

    tag = _verdict_tag(verdict)

    retry_str = (
        _yellow(f"  {retries} retry") if retries else ""
    )
    elapsed_str = _dim(f"  {elapsed:.1f}s")

    print(f"  {tag}  {_dim(ts)}  ran: {ran}{retry_str}{elapsed_str}")

    # Failed checks indented beneath the row
    for fc in row["failed_checks"]:
        name     = fc.get("name", "?")
        observed = fc.get("observed", "?")
        thresh   = fc.get("threshold", "")
        thresh_str = f" (threshold {thresh})" if thresh else ""
        print(
            f"         {_red('!')} {_bold(name)}: "
            f"observed {_yellow(observed)}{_dim(thresh_str)}"
        )


def _render_table(rows: list[dict], check_filter: str | None) -> list[dict]:
    """Print the full table, applying check_filter if given.

    Returns the subset of rows that were actually printed (for summary).
    """
    if check_filter:
        visible = [
            r for r in rows
            if any(
                fc.get("name") == check_filter
                for fc in r["failed_checks"]
            )
        ]
        if not visible:
            print(
                f"\n  No cycles found where {_bold(check_filter)} failed.\n"
            )
            return []
        print(
            f"\n  Filtering to cycles where "
            f"{_bold(check_filter)} failed "
            f"({len(visible)} of {len(rows)} shown):\n"
        )
    else:
        visible = rows
        print()

    for row in visible:
        _render_row(row)

    return visible


def _render_summary(all_rows: list[dict], visible_rows: list[dict]) -> None:
    """Print the summary line: pass rate + most frequent failing check."""
    print(f"\n  {'─' * (_W - 2)}")

    total   = len(all_rows)
    n_pass  = sum(1 for r in all_rows if r["verdict"] == "pass")
    n_fail  = total - n_pass
    pct     = (n_pass / total * 100) if total else 0.0

    pct_str = (
        _green(f"{pct:.0f}%") if pct >= 80
        else _yellow(f"{pct:.0f}%") if pct >= 50
        else _red(f"{pct:.0f}%")
    )
    print(
        f"  Pass rate (last {total}): {pct_str}  "
        f"({n_pass} pass, {_red(str(n_fail)) if n_fail else '0'} fail/degraded)"
    )

    # Most frequently failing check across all rows
    check_counter: Counter = Counter()
    for row in all_rows:
        for fc in row["failed_checks"]:
            name = fc.get("name")
            if name:
                check_counter[name] += 1

    if check_counter:
        top_check, top_count = check_counter.most_common(1)[0]
        print(
            f"  Most frequent failure: {_bold(_red(top_check))} "
            f"({top_count} cycle{'s' if top_count != 1 else ''})"
        )
    else:
        print(f"  Most frequent failure: {_dim('none')}")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m edgedash.verdicts",
        description="Read-only verification history from cycle_log.",
    )
    parser.add_argument(
        "--check",
        metavar="NAME",
        default=None,
        help=(
            "Filter to cycles where this specific check failed. "
            "Example: --check check_freshness"
        ),
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    storage.init_db(cfg.db_path)

    # Read-only: fetch last 20 orchestrator-level cycles.
    # get_recent_cycle_log returns all agents; _build_rows filters to
    # orchestrator summary rows so we get one row per cycle.
    # Fetch more than 20 raw rows to account for sub-agent rows mixed in.
    raw = storage.get_recent_cycle_log(limit=100)
    rows = _build_rows(raw)[:20]

    print(_bold(f"\n  EdgeDash -- Verification History (last {len(rows)} cycles)"))
    print(_dim(f"  {'─' * (_W - 2)}"))

    if not rows:
        print("\n  No orchestrator cycles logged yet.\n")
        return

    visible = _render_table(rows, args.check)
    _render_summary(rows, visible)


if __name__ == "__main__":
    main()
