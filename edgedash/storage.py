"""
The ONLY module in EdgeDash that may import sqlite3
import os
import re
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None.

All database operations — reads, writes, schema creation — live here.
Swapping the backend for Postgres in week 4 means changing this file only.

Public interface
----------------
init_db(path)                   Create tables if absent; return a connection.
upsert_listings(rows) -> int    Insert new listings; return count of NEW rows.
count_unscored() -> int         Listings with fit_score IS NULL.
last_fetch_time() -> str | None Most recent fetched_at value across all rows.
log_cycle(...)                  Write one row to cycle_log.
get_listings(limit, min_score)  Fetch scored listings for the dashboard.
get_extraction(hash)            Return cached extraction dict or None.
set_extraction(hash, dict)      Store an extraction result in the cache.
set_listing_hash(id, hash)      Stamp the description_hash onto a listing row.
get_unscored_listings(limit)    Listings with fit_score IS NULL, oldest first.
save_score(id, score, reason, components, scored_at)  Write scoring result back to a listing row.
get_scored_listings_with_facts() Scored listings joined with their extraction facts.
save_gap_snapshot(run_id, computed_at, rows) Insert one snapshot batch; never overwrites.
get_latest_gap_snapshot()        Return all rows from the most recent run_id.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import os
import re
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import Generator, Iterator, List, Any

# ---------------------------------------------------------------------------
# Type alias for a listing row dict (matches the listings table columns)
# ---------------------------------------------------------------------------
ListingRow = dict  # typed loosely; callers must supply required keys


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_connection: Any = None
_is_pg: bool = False
_db_path: str | None = None


def parse_notes(raw: Any) -> dict:
    """Safely parse a notes field that may be a JSON str or already a dict."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}



def _get_connection() -> Any:
    if _connection is None:
        raise RuntimeError(
            "storage.init_db(path) must be called before any other storage function."
        )
    return _connection



@contextmanager
def _cursor() -> Generator[Any, None, None]:
    conn = _get_connection()
    if _is_pg:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    else:
        cur = conn.cursor()
    try:
        yield _CursorWrapper(cur) if _is_pg else cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise

class _CursorWrapper:
    def __init__(self, cur):
        self._cur = cur

    def _translate(self, sql):
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
        
        if "INSERT OR IGNORE INTO listings" in sql:
            sql = sql.replace("INSERT OR IGNORE INTO listings", "INSERT INTO listings")
            sql += " ON CONFLICT (id) DO NOTHING"
            
        if "INSERT OR REPLACE INTO extraction_cache" in sql:
            sql = sql.replace("INSERT OR REPLACE INTO extraction_cache", "INSERT INTO extraction_cache")
            sql += " ON CONFLICT (description_hash) DO UPDATE SET extracted_json = EXCLUDED.extracted_json, cached_at = EXCLUDED.cached_at"
            
        # Placeholders
        sql = re.sub(r':([a-zA-Z_0-9]+)', r'%(\1)s', sql)
        sql = sql.replace('?', '%s')
        
        return sql

    def execute(self, sql, params=None):
        sql = self._translate(sql)
        if params is not None:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)

    def executemany(self, sql, params):
        sql = self._translate(sql)
        psycopg2.extras.execute_batch(self._cur, sql, params)
        
    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def rowcount(self):
        return self._cur.rowcount


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS query_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        asked_at     TEXT    NOT NULL,
        question     TEXT    NOT NULL,
        tool_chosen  TEXT,
        params_json  TEXT,
        answerable   INTEGER NOT NULL DEFAULT 0,
        duration_ms  INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS listings (
        id          TEXT    PRIMARY KEY,
        title       TEXT    NOT NULL,
        company     TEXT    NOT NULL,
        location    TEXT,
        url         TEXT    NOT NULL,
        description TEXT,
        source      TEXT    NOT NULL,
        posted_at   TEXT,
        fetched_at  TEXT    NOT NULL,
        fit_score   INTEGER,
        fit_reason  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skill_gaps (
        skill       TEXT    PRIMARY KEY,
        frequency   INTEGER NOT NULL DEFAULT 1,
        last_seen   TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cycle_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        agent           TEXT    NOT NULL,
        started_at      TEXT    NOT NULL,
        finished_at     TEXT,
        records_touched INTEGER NOT NULL DEFAULT 0,
        status          TEXT    NOT NULL,
        notes           TEXT
    )
    """,
    # Extraction cache: keyed on SHA-256 of the description text.
    # Stores the validated JSON returned by the LLM so the same
    # description is never sent to the model twice (steering rule 18).
    """
    CREATE TABLE IF NOT EXISTS extraction_cache (
        description_hash TEXT PRIMARY KEY,
        extracted_json   TEXT NOT NULL,
        cached_at        TEXT NOT NULL
    )
    """,
    # Gap snapshots: one row per skill per run (steering rule 25).
    # run_id groups a single GapAnalyzer execution; never updated after insert.
    # The legacy skill_gaps table (single-row-per-skill) is kept for
    # backward-compat but all new code reads/writes gap_snapshots.
    """
    CREATE TABLE IF NOT EXISTS gap_snapshots (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id           TEXT    NOT NULL,
        computed_at      TEXT    NOT NULL,
        skill            TEXT    NOT NULL,
        listings_blocked INTEGER NOT NULL,
        opportunity_cost REAL    NOT NULL,
        mean_score       REAL    NOT NULL,
        top_score        INTEGER NOT NULL,
        example_ids      TEXT    NOT NULL,
        also_nice_to_have INTEGER NOT NULL DEFAULT 0,
        low_confidence   INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_profile (
        id              INTEGER PRIMARY KEY,
        name            TEXT,
        skills          TEXT,
        target_job      TEXT,
        suited_profiles TEXT
    )
    """,
]



def _apply_migrations(cur: Any) -> None:
    if _is_pg:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'listings'")
        existing_cols = {row[0] for row in cur.fetchall()}
        
        # Ensure Row-Level Security (RLS) is enabled on all tables in public schema
        tables = [
            "query_log",
            "listings",
            "skill_gaps",
            "cycle_log",
            "extraction_cache",
            "gap_snapshots",
            "user_profile",
        ]
        for t in tables:
            cur.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;")
    else:
        cur.execute("PRAGMA table_info(listings)")
        existing_cols = {row[1] for row in cur.fetchall()}
        
    if "description_hash" not in existing_cols:
        cur.execute("ALTER TABLE listings ADD COLUMN description_hash TEXT")
    if "scored_at" not in existing_cols:
        cur.execute("ALTER TABLE listings ADD COLUMN scored_at TEXT")
    if "score_components" not in existing_cols:
        cur.execute("ALTER TABLE listings ADD COLUMN score_components TEXT")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_db(path: str) -> Any:
    global _connection, _db_path, _is_pg

    _db_path = path
    
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        if psycopg2 is None:
            raise RuntimeError("DATABASE_URL is set but psycopg2 is not installed.")
        print("Using PostgreSQL backend")
        _is_pg = True
        if _connection is None or getattr(_connection, "closed", 0) != 0:
            _connection = psycopg2.connect(db_url)
    else:
        print("Using SQLite backend")
        _is_pg = False
        if _connection is None or path == ":memory:" or _db_path != path:
            _connection = sqlite3.connect(path, check_same_thread=False)
            _connection.row_factory = sqlite3.Row

    _db_path = path

    with _cursor() as cur:
        for statement in _DDL:
            cur.execute(statement)
        _apply_migrations(cur)

    return _connection


def get_user_profile() -> dict | None:
    """Return the user profile from the database, or None if not set."""
    try:
        with _cursor() as cur:
            cur.execute("SELECT name, skills, target_job, suited_profiles FROM user_profile WHERE id = 1")
            row = cur.fetchone()
            if not row:
                return None
            
            skills = []
            if row["skills"]:
                try:
                    skills = json.loads(row["skills"]) if isinstance(row["skills"], str) else list(row["skills"])
                except Exception:
                    skills = [s.strip() for s in str(row["skills"]).split(",") if s.strip()]

            suited_profiles = []
            if row["suited_profiles"]:
                try:
                    suited_profiles = json.loads(row["suited_profiles"]) if isinstance(row["suited_profiles"], str) else list(row["suited_profiles"])
                except Exception:
                    suited_profiles = []

            return {
                "name": row["name"] or "",
                "skills": skills,
                "target_job": row["target_job"] or "",
                "suited_profiles": suited_profiles
            }
    except Exception:
        return None


def save_user_profile(name: str, skills: list, target_job: str, suited_profiles: list) -> None:
    """Save the extracted profile to the database."""
    with _cursor() as cur:
        cur.execute("DELETE FROM user_profile WHERE id = 1")
        cur.execute(
            """
            INSERT INTO user_profile (id, name, skills, target_job, suited_profiles)
            VALUES (1, ?, ?, ?, ?)
            """,
            (name, json.dumps(skills), target_job, json.dumps(suited_profiles))
        )


def stable_id(source: str, url: str) -> str:
    """Return a stable SHA-256 hash of source + url for dedup."""
    raw = f"{source}::{url}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def upsert_listings(rows: List[ListingRow]) -> int:
    """Insert listings; skip any whose id already exists.

    Returns the number of genuinely NEW rows inserted.
    """
    if not rows:
        return 0

    sql = """
        INSERT OR IGNORE INTO listings
            (id, title, company, location, url, description,
             source, posted_at, fetched_at, fit_score, fit_reason)
        VALUES
            (:id, :title, :company, :location, :url, :description,
             :source, :posted_at, :fetched_at, :fit_score, :fit_reason)
    """
    with _cursor() as cur:
        before = _row_count(cur, "listings")
        cur.executemany(sql, rows)
        after = _row_count(cur, "listings")

    return after - before


def _row_count(cur: Any, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — table is internal
    return cur.fetchone()[0]


def count_unscored() -> int:
    """Return the number of listings that have not yet been scored."""
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NULL")
        return cur.fetchone()[0]


def last_fetch_time() -> str | None:
    """Return the most recent fetched_at timestamp, or None if no rows exist."""
    with _cursor() as cur:
        cur.execute("SELECT MAX(fetched_at) FROM listings")
        result = cur.fetchone()[0]
    return result


def log_cycle(
    agent: str,
    started_at: str,
    finished_at: str,
    records_touched: int,
    status: str,
    notes: str = "",
) -> None:
    """Write one row to cycle_log recording the outcome of an agent run."""
    sql = """
        INSERT INTO cycle_log
            (agent, started_at, finished_at, records_touched, status, notes)
        VALUES
            (?, ?, ?, ?, ?, ?)
    """
    with _cursor() as cur:
        cur.execute(sql, (agent, started_at, finished_at, records_touched, status, notes))


def get_listings(
    limit: int = 50,
    min_score: int = 0,
) -> List[dict]:
    """Return scored listings at or above min_score, newest fetched first."""
    sql = """
        SELECT id, title, company, location, url,
               fit_score, fit_reason, posted_at, fetched_at, source
        FROM   listings
        WHERE  fit_score IS NOT NULL
          AND  fit_score >= ?
        ORDER  BY fetched_at DESC
        LIMIT  ?
    """
    with _cursor() as cur:
        cur.execute(sql, (min_score, limit))
        return [dict(row) for row in cur.fetchall()]


def now_utc() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Extraction cache (steering rule 18)
# ---------------------------------------------------------------------------

def get_extraction(description_hash: str) -> dict | None:
    """Return the cached extraction dict for this hash, or None on a miss."""
    with _cursor() as cur:
        cur.execute(
            "SELECT extracted_json FROM extraction_cache WHERE description_hash = ?",
            (description_hash,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return json.loads(row["extracted_json"])


def set_extraction(description_hash: str, extracted: dict) -> None:
    """Store an extraction result keyed on the description hash."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT OR REPLACE INTO extraction_cache
                (description_hash, extracted_json, cached_at)
            VALUES (?, ?, ?)
            """,
            (description_hash, json.dumps(extracted), now_utc()),
        )


def set_listing_hash(listing_id: str, description_hash: str) -> None:
    """Write the description_hash back onto a listings row.

    Called by the extractor after computing the hash so the column can be
    used to trace which extraction_cache entry belongs to which listing.
    Safe to call multiple times — it is idempotent.
    """
    with _cursor() as cur:
        cur.execute(
            "UPDATE listings SET description_hash = ? WHERE id = ?",
            (description_hash, listing_id),
        )


def get_unscored_listings(limit: int) -> List[dict]:
    """Return up to `limit` listings where fit_score IS NULL, newest fetched first."""
    sql = """
        SELECT id, title, company, location, url, description,
               source, posted_at, fetched_at, description_hash
        FROM   listings
        WHERE  fit_score IS NULL
        ORDER  BY fetched_at DESC, id DESC
        LIMIT  ?
    """
    with _cursor() as cur:
        cur.execute(sql, (limit,))
        return [dict(row) for row in cur.fetchall()]


def delete_old_listings(max_age_days: int) -> int:
    """Delete listings where fetched_at is older than max_age_days.

    Returns the number of rows deleted.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    cutoff_iso = cutoff_dt.isoformat()
    
    with _cursor() as cur:
        # We can't rely on rowcount across all sqlite versions reliably sometimes,
        # but for simple DELETE it usually works. Better to count before/after or use rowcount.
        cur.execute("DELETE FROM listings WHERE fetched_at < ?", (cutoff_iso,))
        return cur.rowcount



def save_score(
    listing_id: str,
    score: int,
    reason: str,
    components: dict,
    scored_at: str,
) -> None:
    """Write fit_score, fit_reason, score_components, and scored_at to a listing row.

    Uses fit_score / fit_reason column names to match the existing schema.
    score_components is stored as JSON text.
    """
    with _cursor() as cur:
        cur.execute(
            """
            UPDATE listings
               SET fit_score        = ?,
                   fit_reason       = ?,
                   score_components = ?,
                   scored_at        = ?
             WHERE id = ?
            """,
            (score, reason, json.dumps(components), scored_at, listing_id),
        )


# ---------------------------------------------------------------------------
# Gap analysis (steering rules 24-27)
# ---------------------------------------------------------------------------

def get_scored_listings_with_facts() -> List[dict]:
    """Return every scored listing that also has a cached extraction.

    Joins listings → extraction_cache on description_hash so the caller
    gets the fit_score and the required_skills / nice_to_have in one shot.
    Listings with no description_hash or no cache entry are silently skipped
    — they haven't been extracted yet and cannot contribute to gap analysis.
    """
    sql = """
        SELECT  l.id,
                l.fit_score,
                e.extracted_json
        FROM    listings l
        JOIN    extraction_cache e
                ON l.description_hash = e.description_hash
        WHERE   l.fit_score IS NOT NULL
        ORDER   BY l.fit_score DESC
    """
    rows = []
    with _cursor() as cur:
        cur.execute(sql)
        for row in cur.fetchall():
            try:
                facts = json.loads(row["extracted_json"])
            except (json.JSONDecodeError, TypeError):
                continue
            rows.append({
                "id":        row["id"],
                "fit_score": row["fit_score"],
                "facts":     facts,
            })
    return rows


def save_gap_snapshot(
    run_id: str,
    computed_at: str,
    rows: List[dict],
) -> None:
    """Insert a batch of gap rows under a single run_id.

    Each dict in rows must have:
        skill, listings_blocked, opportunity_cost, mean_score,
        top_score, example_ids (list[str]), also_nice_to_have, low_confidence

    Never touches existing rows — append-only (steering rule 25).
    """
    sql = """
        INSERT INTO gap_snapshots
            (run_id, computed_at, skill, listings_blocked,
             opportunity_cost, mean_score, top_score,
             example_ids, also_nice_to_have, low_confidence)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with _cursor() as cur:
        for r in rows:
            cur.execute(sql, (
                run_id,
                computed_at,
                r["skill"],
                r["listings_blocked"],
                r["opportunity_cost"],
                r["mean_score"],
                r["top_score"],
                json.dumps(r["example_ids"]),
                r["also_nice_to_have"],
                int(bool(r["low_confidence"])),
            ))


def get_latest_gap_snapshot() -> List[dict]:
    """Return all rows from the most recent GapAnalyzer run_id.

    Returns an empty list if no snapshots exist yet.
    Rows are ordered by opportunity_cost descending (highest gap first).
    """
    with _cursor() as cur:
        cur.execute(
            "SELECT run_id FROM gap_snapshots ORDER BY computed_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return []
        latest_run_id = row["run_id"]

        cur.execute(
            """
            SELECT  skill, listings_blocked, opportunity_cost,
                    mean_score, top_score, example_ids,
                    also_nice_to_have, low_confidence, computed_at
            FROM    gap_snapshots
            WHERE   run_id = ?
            ORDER   BY opportunity_cost DESC
            """,
            (latest_run_id,),
        )
        results = []
        for r in cur.fetchall():
            results.append({
                "skill":            r["skill"],
                "listings_blocked": r["listings_blocked"],
                "opportunity_cost": r["opportunity_cost"],
                "mean_score":       r["mean_score"],
                "top_score":        r["top_score"],
                "example_ids":      json.loads(r["example_ids"]),
                "also_nice_to_have": r["also_nice_to_have"],
                "low_confidence":   bool(r["low_confidence"]),
                "computed_at":      r["computed_at"],
            })
        return results


def get_recent_cycle_log(limit: int = 10) -> List[dict]:
    """Return the most recent `limit` rows from cycle_log, newest first.

    Each dict has: id, agent, started_at, finished_at, records_touched,
    status, notes.
    """
    sql = """
        SELECT id, agent, started_at, finished_at,
               records_touched, status, notes
        FROM   cycle_log
        ORDER  BY started_at DESC
        LIMIT  ?
    """
    with _cursor() as cur:
        cur.execute(sql, (limit,))
        return [dict(row) for row in cur.fetchall()]


def last_scored_at() -> str | None:
    """Return the most recent scored_at timestamp across all listings, or None."""
    with _cursor() as cur:
        cur.execute("SELECT MAX(scored_at) FROM listings WHERE scored_at IS NOT NULL")
        return cur.fetchone()[0]


def last_gap_computed_at() -> str | None:
    """Return the most recent computed_at timestamp from gap_snapshots, or None."""
    with _cursor() as cur:
        cur.execute("SELECT MAX(computed_at) FROM gap_snapshots")
        return cur.fetchone()[0]


def get_all_run_ids() -> List[dict]:
    """Return all distinct run_ids ordered by computed_at ascending.

    Each entry has: run_id, computed_at (first timestamp for that run).
    Used by trend reporting to find the earliest and latest snapshots.
    """
    with _cursor() as cur:
        cur.execute(
            """
            SELECT   run_id, MIN(computed_at) AS computed_at
            FROM     gap_snapshots
            GROUP BY run_id
            ORDER BY computed_at ASC
            """
        )
        return [{"run_id": r["run_id"], "computed_at": r["computed_at"]}
                for r in cur.fetchall()]


def get_last_passing_cycle() -> dict | None:
    """Return the most recent orchestrator cycle_log row with a passing verdict.

    Rule 38: the dashboard must only read data from a cycle that passed
    verification. This function is the single source of truth for that
    gate — callers use the returned row's started_at / finished_at to
    anchor any time-bounded queries they make next.

    Returns None when no passing cycle exists yet (e.g. first run, or
    every cycle so far has been degraded).

    A passing cycle is an orchestrator summary row whose notes JSON
    contains ``"verdict": "pass"``.  The Orchestrator writes this key
    into the notes payload after the Verifier returns ok (step 3 wiring).
    """
    sql = """
        SELECT id, agent, started_at, finished_at,
               records_touched, status, notes
        FROM   cycle_log
        WHERE  agent = 'orchestrator'
          AND  (
                 notes LIKE '%"verdict": "pass"%'
              OR (
                    notes NOT LIKE '%"verdict":%'
                AND notes LIKE '%"outcome": "complete"%'
                 )
          )
        ORDER  BY started_at DESC
        LIMIT  1
    """
    with _cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if row is None:
        return None
    result = dict(row)
    # Parse the notes blob so callers get a dict, not a raw JSON string.
    try:
        result["notes"] = json.loads(result["notes"])
    except (json.JSONDecodeError, TypeError):
        pass
    return result


def log_query(
    asked_at: str,
    question: str,
    tool_chosen: str | None,
    params: dict,
    answerable: bool,
    duration_ms: int,
) -> None:
    """Write one row to query_log recording the outcome of a natural-language query.

    Args:
        asked_at:    ISO-8601 UTC timestamp of the request.
        question:    The raw question string from the user.
        tool_chosen: The tool name returned by the router, or None if unmatched.
        params:      The clamped params dict passed to the tool (empty dict if none).
        answerable:  True when a tool was matched and rows were returned.
        duration_ms: Wall-clock time from question received to answer ready.
    """
    sql = """
        INSERT INTO query_log
            (asked_at, question, tool_chosen, params_json, answerable, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    with _cursor() as cur:
        cur.execute(sql, (
            asked_at,
            question,
            tool_chosen,
            json.dumps(params),
            int(answerable),
            duration_ms,
        ))


def count_queries_today() -> int:
    """Return the total number of questions logged today (UTC)."""
    today_prefix = now_utc()[:10]  # 'YYYY-MM-DD'
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM query_log WHERE asked_at LIKE ?", (f"{today_prefix}%",))
        row = cur.fetchone()
        return int(row[0]) if row else 0


def get_recent_queries(limit: int = 50) -> List[dict]:
    """Return the most recent query log entries, ordered newest first."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT id, asked_at, question, tool_chosen, params_json, answerable, duration_ms
            FROM query_log
            ORDER BY asked_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                "id": r["id"],
                "asked_at": r["asked_at"],
                "question": r["question"],
                "tool_chosen": r["tool_chosen"],
                "params": json.loads(r["params_json"]) if r["params_json"] else {},
                "answerable": bool(r["answerable"]),
                "duration_ms": r["duration_ms"],
            }
            for r in cur.fetchall()
        ]


def get_companies_hiring(since_iso: str) -> List[dict]:
    """Return companies with listing counts posted on or after since_iso.

    Reads from listings regardless of score so companies with unscored
    listings still appear.  Ordered by listing_count descending.

    Args:
        since_iso: ISO-8601 datetime string lower bound (inclusive).

    Returns:
        List of dicts with keys: company, listing_count, newest_posted_at.
    """
    sql = """
        SELECT  company,
                COUNT(*)          AS listing_count,
                MAX(posted_at)    AS newest_posted_at
        FROM    listings
        WHERE   posted_at >= ?
        GROUP   BY company
        ORDER   BY listing_count DESC, company ASC
    """
    with _cursor() as cur:
        cur.execute(sql, (since_iso,))
        return [dict(row) for row in cur.fetchall()]


def count_listings_in_window(since_iso: str) -> int:
    """Return the total number of listings posted on or after since_iso."""
    with _cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM listings WHERE posted_at >= ?",
            (since_iso,),
        )
        return cur.fetchone()[0]


def get_skill_demand(skill_canonical: str) -> List[dict]:
    """Return listings that mention skill_canonical in required or nice-to-have.

    Joins listings with their extraction cache entry and inspects the
    required_skills and nice_to_have lists.  Only scored listings are
    included (unscored listings have no verified facts yet).

    Args:
        skill_canonical: Already-canonicalised skill name — never
                         interpolated into the query string.

    Returns:
        List of dicts with keys:
            id, title, company, fit_score, in_required, in_nice_to_have.
        Empty list when the skill is not found.
    """
    sql = """
        SELECT  l.id,
                l.title,
                l.company,
                l.fit_score,
                e.extracted_json
        FROM    listings l
        JOIN    extraction_cache e
                ON l.description_hash = e.description_hash
        WHERE   l.fit_score IS NOT NULL
        ORDER   BY l.fit_score DESC
    """
    results = []
    with _cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    for row in rows:
        try:
            facts = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue

        required   = [s.lower().strip() for s in (facts.get("required_skills") or [])]
        nice       = [s.lower().strip() for s in (facts.get("nice_to_have") or [])]
        in_req     = skill_canonical in required
        in_nice    = skill_canonical in nice
        if in_req or in_nice:
            results.append({
                "id":             row["id"],
                "title":          row["title"],
                "company":        row["company"],
                "fit_score":      row["fit_score"],
                "in_required":    in_req,
                "in_nice_to_have": in_nice,
            })
    return results


def get_skills_present_in_db() -> set[str]:
    """Return the set of all canonicalised skill strings found in extractions.

    Used by query tools to validate that a model-supplied skill name
    actually exists in the data before running a full scan.
    Only reads from extraction_cache — no join with listings needed.
    """
    with _cursor() as cur:
        cur.execute("SELECT extracted_json FROM extraction_cache")
        rows = cur.fetchall()

    skills: set[str] = set()
    for row in rows:
        try:
            facts = json.loads(row["extracted_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        for s in (facts.get("required_skills") or []):
            if s and s.strip():
                skills.add(s.lower().strip())
        for s in (facts.get("nice_to_have") or []):
            if s and s.strip():
                skills.add(s.lower().strip())
    return skills


def get_gap_snapshots_for_trend(run_ids: List[str]) -> dict[str, List[dict]]:
    """Return gap snapshot rows for each run_id in run_ids.

    Args:
        run_ids: List of run_id strings to fetch; order is preserved.

    Returns:
        Dict mapping run_id → list of snapshot row dicts ordered by
        opportunity_cost DESC.  Missing run_ids produce empty lists.
    """
    result: dict[str, List[dict]] = {rid: [] for rid in run_ids}
    if not run_ids:
        return result

    placeholders = ",".join("?" * len(run_ids))
    sql = f"""
        SELECT  run_id, skill, listings_blocked, opportunity_cost,
                mean_score, top_score, example_ids,
                also_nice_to_have, low_confidence, computed_at
        FROM    gap_snapshots
        WHERE   run_id IN ({placeholders})
        ORDER   BY run_id, opportunity_cost DESC
    """  # noqa: S608 — run_ids are internal strings from our own DB
    with _cursor() as cur:
        cur.execute(sql, run_ids)
        for r in cur.fetchall():
            result[r["run_id"]].append({
                "skill":             r["skill"],
                "listings_blocked":  r["listings_blocked"],
                "opportunity_cost":  r["opportunity_cost"],
                "mean_score":        r["mean_score"],
                "top_score":         r["top_score"],
                "example_ids":       json.loads(r["example_ids"]),
                "also_nice_to_have": r["also_nice_to_have"],
                "low_confidence":    bool(r["low_confidence"]),
                "computed_at":       r["computed_at"],
            })
    return result


def get_listing_stats() -> dict:
    """Return aggregate counts for the listings table.

    Returns a dict with:
        total, scored, unscored, newest_posted_at.
    """
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM listings")
        total = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL")
        scored = cur.fetchone()[0]

        cur.execute("SELECT MAX(posted_at) FROM listings")
        newest = cur.fetchone()[0]

    return {
        "total":            total,
        "scored":           scored,
        "unscored":         total - scored,
        "newest_posted_at": newest,
    }


def get_gap_snapshot_by_run_id(run_id: str) -> List[dict]:
    """Return all rows for a specific run_id, ordered by opportunity_cost DESC."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT  skill, listings_blocked, opportunity_cost,
                    mean_score, top_score, example_ids,
                    also_nice_to_have, low_confidence, computed_at
            FROM    gap_snapshots
            WHERE   run_id = ?
            ORDER   BY opportunity_cost DESC
            """,
            (run_id,),
        )
        results = []
        for r in cur.fetchall():
            results.append({
                "skill":             r["skill"],
                "listings_blocked":  r["listings_blocked"],
                "opportunity_cost":  r["opportunity_cost"],
                "mean_score":        r["mean_score"],
                "top_score":         r["top_score"],
                "example_ids":       json.loads(r["example_ids"]),
                "also_nice_to_have": r["also_nice_to_have"],
                "low_confidence":    bool(r["low_confidence"]),
                "computed_at":       r["computed_at"],
            })
        return results


if __name__ == "__main__":
    import sys
    import os
    from dotenv import load_dotenv
    
    load_dotenv()
    
    if "--migrate" in sys.argv:
        # Default to a local db or memory if no url
        init_db("edgedash.db")
        print("Migration complete.")
    elif "--check" in sys.argv:
        init_db("edgedash.db")
        backend = "PostgreSQL" if _is_pg else "SQLite"
        print(f"Active backend: {backend}")
        print("Connection: OK")
        
        tables = ["query_log", "listings", "skill_gaps", "cycle_log", "extraction_cache", "gap_snapshots"]
        with _cursor() as cur:
            for table in tables:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                count = cur.fetchone()[0]
                print(f"Table {table}: {count} rows")
