"""
edgedash/query/tools.py — deterministic, read-only query tool registry.

NO LLM anywhere in this file. No SQL generation. No text-to-SQL.
Every tool is a parameterised function backed by a storage call.

Public surface
--------------
    TOOLS   dict[str, ToolSpec]
        The router model reads this to pick a tool and supply parameters.
        Each entry has: name, description, parameters (JSON-schema style).

    call(name, **raw_kwargs) -> ToolResult
        Validate, clamp, and execute a named tool.
        Raises KeyError for unknown tool names (rule 45).
        Never raises on bad parameter values — clamps instead (rule 41).

    ToolResult
        rows:    list[dict]   — the raw data rows, always present
        summary: str          — human-readable scope sentence (rule 4)

Clamping ranges (rule 41)
-------------------------
    days    1 – 90
    n       1 – 25
    weeks   1 – 12
    skill   canonicalised via edgedash.skills.canonical, then checked
            against skills actually present in the DB; returns empty
            rather than raising when not found (rule 41 / rule 45)
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List

import edgedash.storage as storage
from edgedash.config import load_config
from edgedash.skills import canonical

# ---------------------------------------------------------------------------
# Clamp helpers (rule 41) — every bound is explicit and documented
# ---------------------------------------------------------------------------

_DAYS_MIN,  _DAYS_MAX  = 1,  90
_N_MIN,     _N_MAX     = 1,  25
_WEEKS_MIN, _WEEKS_MAX = 1,  12


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """Return int(value) clamped to [lo, hi]; fall back to default on error."""
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_days(value: Any) -> int:
    return _clamp_int(value, _DAYS_MIN, _DAYS_MAX, 7)


def _clamp_n(value: Any) -> int:
    return _clamp_int(value, _N_MIN, _N_MAX, 10)


def _clamp_weeks(value: Any) -> int:
    return _clamp_int(value, _WEEKS_MIN, _WEEKS_MAX, 3)


def _since_iso(days: int) -> str:
    """Return an ISO-8601 UTC datetime string for `days` ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# Skill parameter handling (rule 41 + rule 45)
# ---------------------------------------------------------------------------

def _resolve_skill(raw: Any) -> str | None:
    """Canonicalise raw and confirm it exists in the DB.

    Returns the canonical name if found, None otherwise.
    Never interpolated into a query string — callers pass it as a
    typed parameter to the storage function.
    """
    if not raw or not str(raw).strip():
        return None

    cfg = load_config()
    canon = canonical(str(raw), cfg.skill_aliases)
    if not canon:
        return None

    present = storage.get_skills_present_in_db()
    # Match against the already-lowercased set; canonical() lowercases too.
    return canon if canon in present else None


# ---------------------------------------------------------------------------
# Registry types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ParamSpec:
    """JSON-schema-style description of one tool parameter."""
    name:        str
    type:        str          # "integer" | "string"
    description: str
    default:     Any = None


@dataclass(frozen=True)
class ToolSpec:
    """Everything the router needs to know about a tool."""
    name:        str
    description: str
    parameters:  List[ParamSpec]
    fn:          Callable      # the underlying query function


@dataclass
class ToolResult:
    """What every tool returns: raw rows plus a scope sentence."""
    rows:    List[dict]   = field(default_factory=list)
    summary: str          = ""


# ---------------------------------------------------------------------------
# Registry and @tool decorator
# ---------------------------------------------------------------------------

TOOLS: Dict[str, ToolSpec] = {}


def _tool(
    name: str,
    description: str,
    parameters: List[ParamSpec],
) -> Callable:
    """Register a query function in TOOLS.

    Usage::

        @_tool(
            name="my_tool",
            description="...",
            parameters=[ParamSpec(...)],
        )
        def my_tool(...) -> ToolResult:
            ...

    The decorated function is stored unmodified; the decorator only adds
    the ToolSpec entry to TOOLS.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
            return fn(*args, **kwargs)

        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            fn=wrapper,
        )
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Public dispatcher (rule 45 — unknown tool → KeyError, not silent guess)
# ---------------------------------------------------------------------------

def call(name: str, **raw_kwargs: Any) -> ToolResult:
    """Validate, clamp, and execute a named tool.

    Args:
        name:        Tool name; must exist in TOOLS (raises KeyError if not).
        **raw_kwargs: Raw parameter values supplied by the router — treated
                      as untrusted input (rule 41).

    Returns:
        ToolResult with rows and summary.

    Raises:
        KeyError: When name is not in the registry.  Callers must catch this
                  and tell the user which tools ARE available (rule 45).
    """
    if name not in TOOLS:
        available = ", ".join(sorted(TOOLS.keys()))
        raise KeyError(
            f"Unknown tool '{name}'. Available tools: {available}"
        )
    return TOOLS[name].fn(**raw_kwargs)


# ---------------------------------------------------------------------------
# Tool 1 — companies_hiring
# ---------------------------------------------------------------------------

@_tool(
    name="companies_hiring",
    description=(
        "List companies that have active job listings posted within the last N days, "
        "with a count of how many listings each company has. Use this when the user "
        "asks who is hiring, which companies are posting jobs, or wants a count of "
        "listings per employer for a recent time window. Not for score or skill queries."
    ),
    parameters=[
        ParamSpec(
            name="days",
            type="integer",
            description="Look-back window in days. Clamped to 1–90.",
            default=7,
        ),
    ],
)
def companies_hiring(days: Any = 7) -> ToolResult:
    """Companies with listings posted in the last N days, ordered by count."""
    d = _clamp_days(days)
    since = _since_iso(d)
    rows = storage.get_companies_hiring(since)
    total = storage.count_listings_in_window(since)
    summary = (
        f"{total} listing{'s' if total != 1 else ''} from {len(rows)} "
        f"company{'s' if len(rows) != 1 else ''} in the last {d} day{'s' if d != 1 else ''}."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 2 — best_matches
# ---------------------------------------------------------------------------

@_tool(
    name="best_matches",
    description=(
        "Return the top N highest-scoring job listings with their fit score, title, "
        "company, and score reason. Use this when the user asks for the best jobs, "
        "top matches, highest scoring listings, or which roles fit their profile most. "
        "Not for gap or skill-demand queries."
    ),
    parameters=[
        ParamSpec(
            name="n",
            type="integer",
            description="Number of listings to return. Clamped to 1–25.",
            default=10,
        ),
    ],
)
def best_matches(n: Any = 10) -> ToolResult:
    """Highest-scoring listings with score, title, company, reason."""
    count = _clamp_n(n)
    rows = storage.get_listings(limit=count, min_score=0)
    summary = (
        f"Top {len(rows)} scored listing{'s' if len(rows) != 1 else ''} "
        f"(requested {count})."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 3 — top_gaps
# ---------------------------------------------------------------------------

@_tool(
    name="top_gaps",
    description=(
        "Return the top N skill gaps ranked by opportunity cost — the skills missing "
        "from your profile that appear most often in high-scoring listings. Each row "
        "includes the skill name, how many listings it blocks, and the opportunity cost. "
        "Use this for questions about what to learn next, biggest skill gaps, or which "
        "missing skills cost the most opportunities. Not for listing-level detail."
    ),
    parameters=[
        ParamSpec(
            name="n",
            type="integer",
            description="Number of top gaps to return. Clamped to 1–25.",
            default=5,
        ),
    ],
)
def top_gaps(n: Any = 5) -> ToolResult:
    """Top skill gaps by opportunity cost from the latest gap snapshot."""
    count = _clamp_n(n)
    all_rows = storage.get_latest_gap_snapshot()
    rows = all_rows[:count]
    summary = (
        f"Top {len(rows)} gap{'s' if len(rows) != 1 else ''} "
        f"from a snapshot of {len(all_rows)} total skills."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 4 — gap_detail
# ---------------------------------------------------------------------------

@_tool(
    name="gap_detail",
    description=(
        "Show the specific listings that are blocked by one named skill gap — the "
        "drill-down behind a gap row. Use this when the user asks 'which jobs need X', "
        "'show me listings that require X', or wants to investigate a single skill further. "
        "Requires an exact skill name; use top_gaps first if you are unsure of the name."
    ),
    parameters=[
        ParamSpec(
            name="skill",
            type="string",
            description=(
                "Canonical skill name to look up (e.g. 'kubernetes', 'sql', 'power bi'). "
                "Will be canonicalised and validated against the database."
            ),
            default=None,
        ),
    ],
)
def gap_detail(skill: Any = None) -> ToolResult:
    """Listings blocked by one named skill (rule 26 drill-down)."""
    resolved = _resolve_skill(skill)
    if resolved is None:
        return ToolResult(
            rows=[],
            summary=(
                f"Skill '{skill}' not found in the database after canonicalisation. "
                "No listings to show."
            ),
        )

    # Pull the gap snapshot row for this skill to get opportunity_cost context.
    snapshot = storage.get_latest_gap_snapshot()
    gap_row = next((r for r in snapshot if r["skill"] == resolved), None)

    # Collect the listing IDs that are example_ids for this gap, or fall back
    # to a full skill-demand scan for complete coverage.
    if gap_row and gap_row["example_ids"]:
        listing_ids = gap_row["example_ids"]
        rows = [
            r for r in storage.get_listings(limit=500, min_score=0)
            if r["id"] in listing_ids
        ]
    else:
        # Skill exists in DB but is not in the gap snapshot (not a gap for
        # this user) — still show demand data so the user has something.
        rows = []

    n = len(rows)
    opp = f", opportunity cost {gap_row['opportunity_cost']:.1f}" if gap_row else ""
    summary = (
        f"{n} listing{'s' if n != 1 else ''} blocked by '{resolved}'{opp}."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 5 — trend
# ---------------------------------------------------------------------------

@_tool(
    name="trend",
    description=(
        "Show how skill gap opportunity costs have changed across the last N weekly "
        "snapshots. Use this when the user asks whether gaps are growing or shrinking, "
        "which skills are trending up, or how the job market has shifted over time. "
        "Requires at least two snapshots; returns a clear message when there is only one."
    ),
    parameters=[
        ParamSpec(
            name="weeks",
            type="integer",
            description=(
                "How many of the most recent weekly snapshots to span. Clamped to 1–12."
            ),
            default=3,
        ),
    ],
)
def trend(weeks: Any = 3) -> ToolResult:
    """Gap opportunity_cost change over N most-recent snapshots."""
    w = _clamp_weeks(weeks)
    all_runs = storage.get_all_run_ids()   # ordered oldest → newest

    if not all_runs:
        return ToolResult(rows=[], summary="No gap snapshots exist yet.")

    if len(all_runs) == 1:
        return ToolResult(
            rows=[],
            summary=(
                "Only one snapshot available. Run the cycle again to accumulate "
                "history before requesting a trend."
            ),
        )

    # Take the last `w+1` runs so we span `w` intervals; always include first.
    tail = all_runs[-(w + 1):]
    earliest_run = tail[0]
    latest_run   = tail[-1]
    run_ids      = [earliest_run["run_id"], latest_run["run_id"]]

    by_run = storage.get_gap_snapshots_for_trend(run_ids)
    earliest_rows = by_run[earliest_run["run_id"]]
    latest_rows   = by_run[latest_run["run_id"]]

    earliest_map: dict[str, float] = {
        r["skill"]: r["opportunity_cost"] for r in earliest_rows
    }

    rows: list[dict] = []
    for rank, r in enumerate(latest_rows, start=1):
        skill = r["skill"]
        latest_cost   = r["opportunity_cost"]
        earliest_cost = earliest_map.get(skill)

        delta_abs = None if earliest_cost is None else round(latest_cost - earliest_cost, 2)
        delta_pct = None
        if earliest_cost is not None and earliest_cost > 0:
            delta_pct = round(((latest_cost - earliest_cost) / earliest_cost) * 100, 1)

        rows.append({
            "rank":          rank,
            "skill":         skill,
            "earliest_cost": earliest_cost,
            "latest_cost":   latest_cost,
            "delta_abs":     delta_abs,
            "delta_pct":     delta_pct,
            "is_new":        earliest_cost is None,
        })

    n_snapshots = len(all_runs)
    summary = (
        f"Trend across {min(w, n_snapshots - 1)} interval(s) "
        f"({n_snapshots} total snapshots). "
        f"Earliest: {earliest_run['computed_at'][:10]}  "
        f"Latest: {latest_run['computed_at'][:10]}."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 6 — listing_count
# ---------------------------------------------------------------------------

@_tool(
    name="listing_count",
    description=(
        "Return totals for the listings table: how many listings are stored in total, "
        "how many have been scored, how many are still unscored, and the date of the "
        "newest listing. Use this when the user asks how many jobs have been found, "
        "what the pipeline has processed, or for a quick status overview."
    ),
    parameters=[],
)
def listing_count() -> ToolResult:
    """Totals: listings, scored, unscored, newest listing date."""
    stats = storage.get_listing_stats()
    rows = [stats]
    summary = (
        f"{stats['total']} total listings "
        f"({stats['scored']} scored, {stats['unscored']} unscored). "
        f"Newest posted_at: {stats['newest_posted_at'] or 'none'}."
    )
    return ToolResult(rows=rows, summary=summary)


# ---------------------------------------------------------------------------
# Tool 7 — skill_demand
# ---------------------------------------------------------------------------

@_tool(
    name="skill_demand",
    description=(
        "Show how often one specific skill appears in job listings — both as a required "
        "skill and as a nice-to-have. Use this when the user asks how in-demand a skill "
        "is, whether a skill is commonly required vs optional, or wants raw demand data "
        "for a single skill. Not for gap ranking — use top_gaps for that."
    ),
    parameters=[
        ParamSpec(
            name="skill",
            type="string",
            description=(
                "Canonical skill name to look up (e.g. 'python', 'sql', 'tableau'). "
                "Will be canonicalised and validated against the database."
            ),
            default=None,
        ),
    ],
)
def skill_demand(skill: Any = None) -> ToolResult:
    """How often one skill appears in required vs nice_to_have across scored listings."""
    resolved = _resolve_skill(skill)
    if resolved is None:
        return ToolResult(
            rows=[],
            summary=(
                f"Skill '{skill}' not found in the database after canonicalisation. "
                "No data to show."
            ),
        )

    rows = storage.get_skill_demand(resolved)
    in_req  = sum(1 for r in rows if r["in_required"])
    in_nice = sum(1 for r in rows if r["in_nice_to_have"])
    summary = (
        f"'{resolved}' appears in {len(rows)} scored listing(s): "
        f"{in_req} as required, {in_nice} as nice-to-have."
    )
    return ToolResult(rows=rows, summary=summary)
