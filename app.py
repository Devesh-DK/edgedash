# -*- coding: utf-8 -*-
"""
EdgeDash -- Agent Activity Dashboard (read-only).

Rule 38: All data panels read from the LAST PASSING CYCLE only.
Exception: the activity log shows all cycles (pass/fail/degraded) so the
operator can see what went wrong and when.

No writes. No cycle execution. This is a read-only view of verified data.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables (such as GEMINI_API_KEY) from .env
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.is_file():
    load_dotenv(_env_path)
else:
    load_dotenv()

import streamlit as st

# Always load storage through its public API; never touch sqlite3 directly.
import edgedash.storage as storage
from edgedash.config import load_config
from edgedash.llm import LLMError
from edgedash.query.ask import Answer, ask

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent / "config.yaml"
TTL_SECONDS = 10  # cache TTL -- refresh every 10s without hammering SQLite


# ---------------------------------------------------------------------------
# Premium CSS Theme
# ---------------------------------------------------------------------------

_PREMIUM_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap');

/* ── Root variables ──────────────────────────────────────────────────────── */
:root {
    --bg-primary: #0a0e1a;
    --bg-card: rgba(15, 20, 40, 0.65);
    --bg-card-hover: rgba(20, 28, 55, 0.8);
    --glass-border: rgba(255, 255, 255, 0.06);
    --glass-border-hover: rgba(255, 255, 255, 0.12);
    --text-primary: #e8eaf6;
    --text-secondary: #8892b0;
    --text-muted: #5a6380;
    --accent-blue: #60a5fa;
    --accent-purple: #a78bfa;
    --accent-cyan: #22d3ee;
    --accent-green: #34d399;
    --accent-amber: #fbbf24;
    --accent-rose: #fb7185;
    --gradient-primary: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    --gradient-success: linear-gradient(135deg, #34d399 0%, #059669 100%);
    --gradient-warning: linear-gradient(135deg, #fbbf24 0%, #f59e0b 100%);
    --gradient-danger: linear-gradient(135deg, #fb7185 0%, #e11d48 100%);
    --gradient-info: linear-gradient(135deg, #22d3ee 0%, #0891b2 100%);
    --shadow-card: 0 4px 24px rgba(0, 0, 0, 0.3), 0 1px 4px rgba(0, 0, 0, 0.2);
    --shadow-glow-blue: 0 0 20px rgba(96, 165, 250, 0.15);
    --shadow-glow-purple: 0 0 20px rgba(167, 139, 250, 0.15);
}

/* ── Global resets ───────────────────────────────────────────────────────── */
.stApp {
    background: var(--bg-primary) !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif !important;
}

.stApp > header { background: transparent !important; }

/* Hide Streamlit branding */
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; }

/* Scrollbar */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
    background: rgba(255,255,255,0.1);
    border-radius: 3px;
}
::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.2); }

/* ── Typography ──────────────────────────────────────────────────────────── */
h1, h2, h3, h4, h5, h6 {
    font-family: 'Inter', sans-serif !important;
    color: var(--text-primary) !important;
}

p, span, label, .stMarkdown {
    font-family: 'Inter', sans-serif !important;
}

/* ── Glass card base ─────────────────────────────────────────────────────── */
.glass-card {
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 24px;
    box-shadow: var(--shadow-card);
    transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

.glass-card:hover {
    background: var(--bg-card-hover);
    border-color: var(--glass-border-hover);
    transform: translateY(-2px);
    box-shadow: var(--shadow-card), var(--shadow-glow-blue);
}

/* ── Metric cards ────────────────────────────────────────────────────────── */
.metric-card {
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 20px 24px;
    box-shadow: var(--shadow-card);
    transition: all 0.35s cubic-bezier(0.4, 0, 0.2, 1);
    position: relative;
    overflow: hidden;
}

.metric-card::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    border-radius: 16px 16px 0 0;
    opacity: 0;
    transition: opacity 0.35s ease;
}

.metric-card:hover {
    border-color: var(--glass-border-hover);
    transform: translateY(-3px);
}

.metric-card:hover::before { opacity: 1; }

.metric-card.blue::before  { background: var(--gradient-primary); }
.metric-card.green::before { background: var(--gradient-success); }
.metric-card.amber::before { background: var(--gradient-warning); }
.metric-card.cyan::before  { background: var(--gradient-info); }

.metric-card .metric-icon {
    width: 40px;
    height: 40px;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 18px;
    margin-bottom: 12px;
}

.metric-card .metric-icon.blue  { background: rgba(96, 165, 250, 0.12); color: var(--accent-blue); }
.metric-card .metric-icon.green { background: rgba(52, 211, 153, 0.12); color: var(--accent-green); }
.metric-card .metric-icon.amber { background: rgba(251, 191, 36, 0.12); color: var(--accent-amber); }
.metric-card .metric-icon.cyan  { background: rgba(34, 211, 238, 0.12); color: var(--accent-cyan); }

.metric-card .metric-label {
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 4px;
}

.metric-card .metric-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-primary);
    line-height: 1.2;
}

/* ── Section headers ─────────────────────────────────────────────────────── */
.section-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 20px;
    padding-bottom: 12px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
}

.section-header .section-icon {
    width: 36px;
    height: 36px;
    border-radius: 10px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
}

.section-header .section-title {
    font-size: 1.15rem;
    font-weight: 700;
    color: var(--text-primary);
    margin: 0;
}

.section-header .section-subtitle {
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin: 0;
}

/* ── Activity log rows ───────────────────────────────────────────────────── */
.activity-row {
    background: var(--bg-card);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    padding: 12px 16px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    transition: all 0.25s ease;
}

.activity-row:hover {
    background: var(--bg-card-hover);
    border-color: var(--glass-border-hover);
}

.status-badge {
    font-size: 0.68rem;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 6px;
    letter-spacing: 0.05em;
    min-width: 78px;
    text-align: center;
    color: #fff;
}

.verdict-pill {
    font-size: 0.65rem;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 20px;
    letter-spacing: 0.04em;
    color: #fff;
}

.activity-ts {
    font-size: 0.82rem;
    color: var(--text-secondary);
    min-width: 150px;
    font-variant-numeric: tabular-nums;
}

.activity-detail {
    font-size: 0.82rem;
    color: var(--text-muted);
}

.activity-detail b { color: var(--text-secondary); }

.activity-elapsed {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
    margin-left: auto;
}

.activity-fail-line {
    width: 100%;
    padding: 4px 0 0 90px;
    font-size: 0.8rem;
    color: var(--accent-rose);
}

/* ── Listing cards ───────────────────────────────────────────────────────── */
.listing-card {
    background: var(--bg-card);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    padding: 14px 18px;
    margin-bottom: 8px;
    display: flex;
    align-items: flex-start;
    gap: 14px;
    transition: all 0.25s ease;
}

.listing-card:hover {
    background: var(--bg-card-hover);
    border-color: var(--glass-border-hover);
    transform: translateX(4px);
}

.score-ring {
    width: 44px;
    height: 44px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    font-weight: 700;
    color: #fff;
    flex-shrink: 0;
}

.listing-info { flex: 1; min-width: 0; }

.listing-title {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-primary);
    text-decoration: none;
    transition: color 0.2s ease;
}

.listing-title:hover { color: var(--accent-blue); }

.listing-meta {
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin-top: 3px;
    line-height: 1.4;
}

/* ── Gap cards ───────────────────────────────────────────────────────────── */
.gap-card {
    background: var(--bg-card);
    border: 1px solid var(--glass-border);
    border-radius: 12px;
    padding: 14px 18px;
    margin-bottom: 8px;
    display: flex;
    align-items: flex-start;
    gap: 14px;
    transition: all 0.25s ease;
}

.gap-card:hover {
    background: var(--bg-card-hover);
    border-color: var(--glass-border-hover);
    transform: translateX(4px);
}

.gap-rank {
    width: 32px;
    height: 32px;
    border-radius: 8px;
    background: rgba(167, 139, 250, 0.12);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.8rem;
    font-weight: 700;
    color: var(--accent-purple);
    flex-shrink: 0;
}

.gap-info { flex: 1; min-width: 0; }

.gap-skill {
    font-size: 0.9rem;
    font-weight: 600;
    color: var(--text-primary);
}

.gap-stats {
    font-size: 0.78rem;
    color: var(--text-secondary);
    margin-top: 3px;
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
}

.gap-stat-pill {
    background: rgba(255,255,255,0.04);
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 0.72rem;
    color: var(--text-secondary);
}

.gap-low-conf {
    font-size: 0.7rem;
    color: var(--accent-amber);
    font-style: italic;
}

/* ── Ask panel ───────────────────────────────────────────────────────────── */
.ask-container {
    background: var(--bg-card);
    backdrop-filter: blur(16px);
    border: 1px solid var(--glass-border);
    border-radius: 16px;
    padding: 28px;
    box-shadow: var(--shadow-card);
}

.example-chip {
    display: inline-block;
    background: rgba(96, 165, 250, 0.08);
    border: 1px solid rgba(96, 165, 250, 0.2);
    border-radius: 20px;
    padding: 6px 16px;
    font-size: 0.8rem;
    color: var(--accent-blue);
    cursor: pointer;
    transition: all 0.2s ease;
    margin: 4px 4px 4px 0;
}

.example-chip:hover {
    background: rgba(96, 165, 250, 0.15);
    border-color: rgba(96, 165, 250, 0.4);
    transform: translateY(-1px);
}

/* ── Answer display ──────────────────────────────────────────────────────── */
.answer-box {
    background: rgba(96, 165, 250, 0.04);
    border: 1px solid rgba(96, 165, 250, 0.12);
    border-radius: 12px;
    padding: 18px 20px;
    margin-top: 16px;
}

.answer-box .answer-text {
    font-size: 0.92rem;
    color: var(--text-primary);
    line-height: 1.6;
}

.answer-provenance {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-top: 10px;
    padding-top: 10px;
    border-top: 1px solid rgba(255,255,255,0.04);
}

.answer-provenance code {
    background: rgba(255,255,255,0.06);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 0.72rem;
}

/* ── Legend strip ────────────────────────────────────────────────────────── */
.legend-strip {
    display: flex;
    gap: 20px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}

.legend-item {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.76rem;
    color: var(--text-secondary);
}

.legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 3px;
}

/* ── Dashboard title ─────────────────────────────────────────────────────── */
.dash-title {
    font-size: 1.8rem;
    font-weight: 800;
    background: linear-gradient(135deg, #e8eaf6, #a78bfa, #60a5fa);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 2px;
    letter-spacing: -0.02em;
}

.dash-subtitle {
    font-size: 0.82rem;
    color: var(--text-muted);
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 24px;
}

.dash-subtitle .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent-green);
    animation: pulse-dot 2s infinite ease-in-out;
}

@keyframes pulse-dot {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.5; transform: scale(0.8); }
}

/* ── Staleness warnings ──────────────────────────────────────────────────── */
.stale-warn {
    background: rgba(251, 191, 36, 0.06);
    border: 1px solid rgba(251, 191, 36, 0.2);
    border-radius: 12px;
    padding: 14px 18px;
    font-size: 0.85rem;
    color: var(--accent-amber);
    margin: 12px 0;
}

.stale-err {
    background: rgba(251, 113, 133, 0.06);
    border: 1px solid rgba(251, 113, 133, 0.2);
    border-radius: 12px;
    padding: 14px 18px;
    font-size: 0.85rem;
    color: var(--accent-rose);
    margin: 12px 0;
}

/* ── Footer ──────────────────────────────────────────────────────────────── */
.dash-footer {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.74rem;
    padding: 24px 0 12px;
    border-top: 1px solid rgba(255,255,255,0.04);
    margin-top: 32px;
}

/* ── Streamlit overrides ─────────────────────────────────────────────────── */
.stMetric { display: none !important; }

div[data-testid="stHorizontalBlock"] {
    gap: 16px !important;
}

/* Clean dividers */
hr {
    border: none !important;
    border-top: 1px solid rgba(255,255,255,0.04) !important;
    margin: 28px 0 !important;
}

/* Text input styling */
.stTextInput > div > div > input {
    background: rgba(15, 20, 40, 0.8) !important;
    border: 1px solid rgba(255,255,255,0.08) !important;
    border-radius: 12px !important;
    color: var(--text-primary) !important;
    font-family: 'Inter', sans-serif !important;
    padding: 12px 16px !important;
    font-size: 0.9rem !important;
    transition: border-color 0.2s ease !important;
}

.stTextInput > div > div > input:focus {
    border-color: rgba(96, 165, 250, 0.4) !important;
    box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.1) !important;
}

.stTextInput > div > div > input::placeholder {
    color: var(--text-muted) !important;
}

/* Button styling */
.stButton > button {
    background: rgba(96, 165, 250, 0.08) !important;
    border: 1px solid rgba(96, 165, 250, 0.2) !important;
    border-radius: 10px !important;
    color: var(--accent-blue) !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 500 !important;
    font-size: 0.82rem !important;
    padding: 8px 16px !important;
    transition: all 0.2s ease !important;
}

.stButton > button:hover {
    background: rgba(96, 165, 250, 0.15) !important;
    border-color: rgba(96, 165, 250, 0.4) !important;
    transform: translateY(-1px) !important;
}

.stButton > button:disabled {
    opacity: 0.4 !important;
    transform: none !important;
}

/* Dataframe styling */
.stDataFrame {
    border-radius: 12px !important;
    overflow: hidden !important;
    border: 1px solid rgba(255,255,255,0.06) !important;
}

/* Spinner */
.stSpinner > div {
    color: var(--accent-blue) !important;
}

/* Alert boxes */
.stAlert {
    border-radius: 12px !important;
}

/* Expander */
.streamlit-expanderHeader {
    background: var(--bg-card) !important;
    border-radius: 12px !important;
}

/* Animate cards on load */
@keyframes fadeSlideUp {
    from { opacity: 0; transform: translateY(12px); }
    to   { opacity: 1; transform: translateY(0); }
}

.metric-card, .activity-row, .listing-card, .gap-card {
    animation: fadeSlideUp 0.4s ease forwards;
}

.activity-row:nth-child(1)  { animation-delay: 0ms; }
.activity-row:nth-child(2)  { animation-delay: 30ms; }
.activity-row:nth-child(3)  { animation-delay: 60ms; }
.activity-row:nth-child(4)  { animation-delay: 90ms; }
.activity-row:nth-child(5)  { animation-delay: 120ms; }
.activity-row:nth-child(6)  { animation-delay: 150ms; }
.activity-row:nth-child(7)  { animation-delay: 180ms; }
.activity-row:nth-child(8)  { animation-delay: 210ms; }
.activity-row:nth-child(9)  { animation-delay: 240ms; }
.activity-row:nth-child(10) { animation-delay: 270ms; }

</style>
"""


# ---------------------------------------------------------------------------
# Cached data loaders (rule 38 -- only from passing cycles except activity log)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=TTL_SECONDS)
def _load_config():
    """Load EdgeDash config from config.yaml."""
    return load_config(CONFIG_PATH)


def _init():
    """Initialise storage once per loader call."""
    cfg = _load_config()
    storage.init_db(cfg.db_path)
    return cfg


@st.cache_data(ttl=TTL_SECONDS)
def load_last_passing_cycle() -> dict | None:
    """Return the most recent cycle that passed verification, or None."""
    _init()
    return storage.get_last_passing_cycle()


@st.cache_data(ttl=TTL_SECONDS)
def load_recent_cycles(limit: int = 30) -> list[dict]:
    """Return the N most recent cycles (all statuses) for the activity log."""
    _init()
    return storage.get_recent_cycle_log(limit)


@st.cache_data(ttl=TTL_SECONDS)
def load_top_listings(limit: int = 10) -> list[dict]:
    """Return top-scored listings (all statuses -- dashboard filters by score)."""
    _init()
    return storage.get_listings(limit=limit, min_score=0)


@st.cache_data(ttl=TTL_SECONDS)
def load_top_gaps(limit: int = 10) -> list[dict]:
    """Return top skill gaps from the latest gap snapshot."""
    _init()
    gaps = storage.get_latest_gap_snapshot()
    return gaps[:limit] if gaps else []


@st.cache_data(ttl=TTL_SECONDS)
def load_summary_counts() -> tuple[int, int]:
    """Return (total_listings, scored_listings)."""
    _init()
    with storage._cursor() as cur:  # noqa: SLF001
        cur.execute("SELECT COUNT(*) FROM listings")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NOT NULL")
        scored = cur.fetchone()[0]
    return total, scored


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_IST = timezone(timedelta(hours=5, minutes=30))


def fmt_ts(ts_str: str | None) -> str:
    """Convert ISO timestamp to readable format in IST, or 'never'."""
    if not ts_str:
        return "never"
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_ist = dt.astimezone(_IST)
        return dt_ist.strftime("%Y-%m-%d %I:%M %p IST")
    except (ValueError, AttributeError):
        return str(ts_str)


def verdict_label(v: str) -> str:
    """Map verdict string to a display label."""
    return {
        "pass":     "PASS",
        "fail":     "FAIL",
        "degraded": "DEGRADED",
        "unknown":  "?",
    }.get(v, v.upper() if v else "?")


def parse_notes(raw) -> dict:
    """Safely parse a notes field that may be a str or already a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _esc(text: str) -> str:
    """Escape HTML special characters for safe embedding."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def render_header(last_passing: dict | None, all_cycles: list[dict]) -> None:
    """Render the header strip with glassmorphism metric cards."""
    total, scored = load_summary_counts()

    newest_ts    = all_cycles[0]["started_at"] if all_cycles else None
    last_pass_ts = last_passing["started_at"] if last_passing else None

    if last_passing:
        notes   = parse_notes(last_passing.get("notes"))
        verdict = notes.get("verdict", "unknown")
    else:
        verdict = "unknown"

    verdict_icon = {"pass": "✓", "fail": "✗", "degraded": "⚠", "unknown": "?"}.get(verdict, "?")
    verdict_color_class = {"pass": "green", "fail": "rose", "degraded": "amber", "unknown": ""}.get(verdict, "")

    cards_html = f"""
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:8px">
        <div class="metric-card blue">
            <div class="metric-icon blue">🕐</div>
            <div class="metric-label">Last Verified Cycle</div>
            <div class="metric-value" style="font-size:1rem">{_esc(fmt_ts(last_pass_ts))}</div>
        </div>
        <div class="metric-card green">
            <div class="metric-icon green">📋</div>
            <div class="metric-label">Total Listings</div>
            <div class="metric-value">{total:,}</div>
        </div>
        <div class="metric-card amber">
            <div class="metric-icon amber">⚡</div>
            <div class="metric-label">Scored Listings</div>
            <div class="metric-value">{scored:,}</div>
        </div>
        <div class="metric-card cyan">
            <div class="metric-icon cyan">{verdict_icon}</div>
            <div class="metric-label">Verdict</div>
            <div class="metric-value">{verdict_label(verdict)}</div>
        </div>
    </div>
    """
    st.markdown(cards_html, unsafe_allow_html=True)

    # Staleness warning -- never silently show unverified data (rule 38)
    if not last_passing and all_cycles:
        st.markdown(
            '<div class="stale-err">'
            "⚠ No verified cycles exist yet. Every cycle so far has failed or been "
            "degraded. The listings and gaps panels below are empty until a cycle "
            "passes verification."
            "</div>",
            unsafe_allow_html=True,
        )
    elif newest_ts and last_pass_ts and newest_ts != last_pass_ts:
        st.markdown(
            f'<div class="stale-warn">'
            f"⚠ The newest cycle ({_esc(fmt_ts(newest_ts))}) did not pass verification. "
            f"All data panels reflect the last verified cycle from "
            f"<b>{_esc(fmt_ts(last_pass_ts))}</b>. "
            f"Stale verified data always beats fresh unverified data (rule 38)."
            f"</div>",
            unsafe_allow_html=True,
        )


def _row_style(outcome: str) -> tuple[str, str, str]:
    """Return (border_color, badge_bg, badge_text) for an outcome."""
    return {
        "complete":      ("#34d399", "var(--gradient-success)", "COMPLETE"),
        "partial":       ("#fbbf24", "var(--gradient-warning)", "PARTIAL"),
        "degraded":      ("#fb7185", "var(--gradient-danger)", "DEGRADED"),
        "nothing_to_do": ("#22d3ee", "var(--gradient-info)", "IDLE"),
        "dry_run":       ("#8892b0", "linear-gradient(135deg,#6c757d,#495057)", "DRY RUN"),
    }.get(outcome, ("#8892b0", "linear-gradient(135deg,#6c757d,#495057)", outcome.upper() or "?"))


def render_activity_log(all_cycles: list[dict]) -> None:
    """Render the main agent activity log with premium glass cards."""
    st.markdown(
        '<div class="section-header">'
        '<div class="section-icon" style="background:rgba(96,165,250,0.12);color:var(--accent-blue)">📊</div>'
        '<div>'
        '<p class="section-title">Agent Activity Log</p>'
        '<p class="section-subtitle">Most recent 30 cycles — includes all outcomes</p>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    if not all_cycles:
        st.info("No cycles logged yet.")
        return

    # Legend strip
    st.markdown(
        '<div class="legend-strip">'
        '<div class="legend-item"><div class="legend-dot" style="background:#34d399"></div>complete</div>'
        '<div class="legend-item"><div class="legend-dot" style="background:#fbbf24"></div>partial</div>'
        '<div class="legend-item"><div class="legend-dot" style="background:#fb7185"></div>degraded / failed</div>'
        '<div class="legend-item"><div class="legend-dot" style="background:#22d3ee"></div>idle</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    rows_html_parts = []
    for cycle in all_cycles:
        notes = parse_notes(cycle.get("notes"))

        outcome       = notes.get("outcome", cycle.get("status", "unknown"))
        verdict_str   = notes.get("verdict", "--")
        retry_count   = int(notes.get("retry_count", 0))
        elapsed       = float(notes.get("elapsed_s", 0.0))
        ran           = notes.get("ran", [])
        skipped       = notes.get("skipped", {})
        failures_list = notes.get("failures", [])
        ts            = fmt_ts(cycle.get("started_at"))

        # First failing check with observed value (rule 37)
        failed_check_str = ""
        if verdict_str in ("fail", "degraded"):
            fc = notes.get("failed_checks", [])
            if fc and isinstance(fc, list):
                first = fc[0]
                failed_check_str = (
                    f"{first.get('name', '?')}: {first.get('observed', '?')}"
                )
            elif failures_list:
                failed_check_str = failures_list[0][:80]

        _, badge_bg, badge_label = _row_style(outcome)

        ran_str     = _esc(", ".join(ran) if ran else "—")
        skipped_str = _esc(", ".join(skipped.keys()) if skipped else "—")
        retry_str   = f"  · {retry_count} retry" if retry_count else ""

        verdict_color = {
            "pass":     "#34d399",
            "fail":     "#fb7185",
            "degraded": "#fb7185",
            "unknown":  "#8892b0",
            "--":       "#8892b0",
        }.get(verdict_str, "#8892b0")

        failed_html = (
            f'<div class="activity-fail-line">⚠ {_esc(failed_check_str)}</div>'
            if failed_check_str else ""
        )

        rows_html_parts.append(f"""
        <div class="activity-row">
            <span class="status-badge" style="background:{badge_bg}">{badge_label}</span>
            <span class="activity-ts">{_esc(ts)}</span>
            <span class="activity-detail"><b>ran:</b> {ran_str}</span>
            <span class="activity-detail"><b>skipped:</b> {skipped_str}</span>
            <span class="verdict-pill" style="background:{verdict_color}">{verdict_label(verdict_str)}</span>
            <span class="activity-elapsed">{elapsed:.1f}s{retry_str}</span>
            {failed_html}
        </div>
        """)

    st.markdown("".join(rows_html_parts), unsafe_allow_html=True)


def render_listings_panel() -> None:
    """Render top 10 scored listings with premium glass cards."""
    st.markdown(
        '<div class="section-header">'
        '<div class="section-icon" style="background:rgba(52,211,153,0.12);color:var(--accent-green)">💼</div>'
        '<div>'
        '<p class="section-title">Top Scored Listings</p>'
        '<p class="section-subtitle">Best job matches by fit score</p>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    listings = load_top_listings(limit=10)
    if not listings:
        st.info("No scored listings yet.")
        return

    cards_html_parts = []
    for listing in listings:
        score   = listing.get("fit_score", 0) or 0
        title   = _esc(listing.get("title", "Untitled"))
        company = _esc(listing.get("company", "Unknown"))
        reason  = _esc((listing.get("fit_reason", "") or "")[:100])
        url     = _esc(listing.get("url", "#"))
        src     = _esc((listing.get("source") or "arbeitnow").upper())

        if score >= 80:
            ring_bg = "var(--gradient-success)"
        elif score >= 60:
            ring_bg = "var(--gradient-warning)"
        else:
            ring_bg = "var(--gradient-danger)"

        cards_html_parts.append(f"""
        <div class="listing-card">
            <div class="score-ring" style="background:{ring_bg}">{score}</div>
            <div class="listing-info">
                <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
                    <a href="{url}" target="_blank" class="listing-title">{title}</a>
                    <span style="background:rgba(96,165,250,0.12);color:var(--accent-blue);font-size:0.65rem;font-weight:700;padding:2px 7px;border-radius:4px;letter-spacing:0.04em">{src}</span>
                </div>
                <div class="listing-meta">{company} — {reason}</div>
            </div>
        </div>
        """)

    st.markdown("".join(cards_html_parts), unsafe_allow_html=True)


def render_gaps_panel() -> None:
    """Render top 10 skill gaps with premium glass cards."""
    st.markdown(
        '<div class="section-header">'
        '<div class="section-icon" style="background:rgba(167,139,250,0.12);color:var(--accent-purple)">🎯</div>'
        '<div>'
        '<p class="section-title">Top Skill Gaps</p>'
        '<p class="section-subtitle">Skills blocking the most opportunities</p>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    gaps = load_top_gaps(limit=10)
    if not gaps:
        st.info("No gap analysis yet.")
        return

    cards_html_parts = []
    for i, gap in enumerate(gaps, 1):
        skill       = _esc(gap.get("skill", "unknown"))
        blocked     = gap.get("listings_blocked", 0)
        cost        = gap.get("opportunity_cost", 0.0)
        mean_score  = gap.get("mean_score", 0.0)
        low_conf    = gap.get("low_confidence", False)

        low_conf_html = '<span class="gap-low-conf">low confidence</span>' if low_conf else ""

        cards_html_parts.append(f"""
        <div class="gap-card">
            <div class="gap-rank">#{i}</div>
            <div class="gap-info">
                <div class="gap-skill">{skill} {low_conf_html}</div>
                <div class="gap-stats">
                    <span class="gap-stat-pill">🚫 {blocked} blocked</span>
                    <span class="gap-stat-pill">💰 cost {cost:.1f}</span>
                    <span class="gap-stat-pill">📊 mean {mean_score:.0f}</span>
                </div>
            </div>
        </div>
        """)

    st.markdown("".join(cards_html_parts), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# "Ask your data" panel (rules 42-45)
# ---------------------------------------------------------------------------

_EXAMPLE_QUESTIONS = [
    "Which companies are hiring right now?",
    "What are my top 5 skill gaps?",
    "Show me my 10 best job matches.",
]

_SESSION_MAX_QUESTIONS = 10
_SESSION_WINDOW_SECONDS = 600  # 10 minutes


def _run_ask(question: str, cfg) -> None:
    """Execute ask(), display the answer text and rows. Called from render_ask_panel."""
    with st.spinner("Thinking…"):
        try:
            answer: Answer = ask(question, cfg)
        except LLMError as exc:
            st.error(f"The query pipeline hit an error: {exc}")
            return

    # Check if this was a rejection notice (e.g. length exceeded)
    reason = answer.params.get("reason", "")
    if reason.startswith("rejected:") and not answer.tool_used and not answer.rows:
        if reason == "rejected: suspicious input":
            # Rule: standard can't-answer message, do not explain the filter
            st.markdown(answer.text)
        else:
            st.warning(answer.text)
        return

    # Build the answer in the premium answer box
    answer_html = f'<div class="answer-box"><div class="answer-text">{_esc(answer.text)}</div>'

    if answer.tool_used:
        params_str = ", ".join(f"{k}={v}" for k, v in answer.params.items())
        prov = f'<code>{_esc(answer.tool_used)}</code>'
        if params_str:
            prov += f' · <code>{_esc(params_str)}</code>'
        answer_html += f'<div class="answer-provenance">Tool: {prov}</div>'

    answer_html += '</div>'
    st.markdown(answer_html, unsafe_allow_html=True)

    # Raw rows — always shown per rule 44 (no prose without the data)
    if answer.rows:
        st.markdown(
            '<p style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);'
            'margin:16px 0 8px">Underlying data</p>',
            unsafe_allow_html=True,
        )
        st.dataframe(answer.rows, use_container_width=True)
    else:
        st.info("No rows returned for this question.")


def render_ask_panel(cfg) -> None:
    """Render the 'Ask your data' section with premium glassmorphism.

    Includes abuse guards:
      - Global daily cap (ask box disabled on exceed; dashboard unaffected)
      - Session rate limiter (max 10 questions per 10 minutes)
      - Input guards & injection filters handled in ask()
    """
    st.markdown(
        '<div class="section-header">'
        '<div class="section-icon" style="background:rgba(96,165,250,0.12);color:var(--accent-blue)">💬</div>'
        '<div>'
        '<p class="section-title">Ask Your Data</p>'
        '<p class="section-subtitle">Natural language queries powered by AI — answers from your actual data only</p>'
        '</div></div>',
        unsafe_allow_html=True,
    )

    # ── Global daily cap check ───────────────────────────────────────────────
    today_count = 0
    try:
        today_count = storage.count_queries_today()
    except Exception:
        pass

    if today_count >= cfg.daily_ask_cap:
        st.markdown(
            f'<div class="stale-warn">'
            f"Daily question limit reached ({today_count}/{cfg.daily_ask_cap} questions today). "
            f"The ask feature is temporarily disabled to preserve API quota. "
            f"All other dashboard panels remain fully active."
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown("**Try an example:**")
        btn_cols = st.columns(len(_EXAMPLE_QUESTIONS))
        for col, example in zip(btn_cols, _EXAMPLE_QUESTIONS):
            with col:
                st.button(example, use_container_width=True, key=f"ex_{example}", disabled=True)
        st.text_input(
            "Or type your own question:",
            value="",
            placeholder="Daily limit reached. Please check back tomorrow.",
            key="ask_input_disabled",
            disabled=True,
        )
        return

    # ── Example questions as clickable buttons ───────────────────────────────
    st.markdown(
        '<p style="font-size:0.82rem;font-weight:600;color:var(--text-secondary);'
        'margin-bottom:8px">Try an example:</p>',
        unsafe_allow_html=True,
    )
    btn_cols = st.columns(len(_EXAMPLE_QUESTIONS))
    clicked_example: str | None = None
    for col, example in zip(btn_cols, _EXAMPLE_QUESTIONS):
        with col:
            if st.button(example, use_container_width=True, key=f"ex_{example}"):
                clicked_example = example

    # ── Free-form text input ─────────────────────────────────────────────────
    typed_question = st.text_input(
        "Or type your own question:",
        placeholder="e.g. How many listings have been scored?",
        key="ask_input",
        label_visibility="collapsed",
    )

    # Typed question takes priority; example button is the fallback.
    question = typed_question.strip() or clicked_example

    if question:
        # ── Session rate limiter: max 10 questions per 10 minutes ─────────────
        now = time.time()
        if "ask_timestamps" not in st.session_state:
            st.session_state["ask_timestamps"] = []

        valid_ts = [t for t in st.session_state["ask_timestamps"] if now - t < _SESSION_WINDOW_SECONDS]
        st.session_state["ask_timestamps"] = valid_ts

        if len(valid_ts) >= _SESSION_MAX_QUESTIONS:
            oldest = min(valid_ts)
            wait_sec = max(1, int(_SESSION_WINDOW_SECONDS - (now - oldest)))
            mins = wait_sec // 60
            secs = wait_sec % 60
            wait_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            st.markdown(
                f'<div class="stale-warn">'
                f"Rate limit reached: you can ask at most {_SESSION_MAX_QUESTIONS} questions per 10 minutes. "
                f"Please wait <b>{wait_str}</b> before asking another question."
                f"</div>",
                unsafe_allow_html=True,
            )
            return

        # Record timestamp before proceeding with query
        st.session_state["ask_timestamps"].append(now)
        _run_ask(question, cfg)
    else:
        st.markdown(
            '<div style="text-align:center;color:var(--text-muted);font-size:0.85rem;'
            'padding:16px 0">'
            '💡 Click an example above or type a question to get started'
            '</div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(
        page_title="EdgeDash — Agent Activity",
        page_icon="⚡",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    # Inject premium CSS
    st.markdown(_PREMIUM_CSS, unsafe_allow_html=True)

    # Dashboard title
    st.markdown(
        '<div class="dash-title">EdgeDash</div>'
        '<div class="dash-subtitle">'
        '<span class="dot"></span>'
        'Live · Read-only · Refreshes every 10s · Verified data only'
        '</div>',
        unsafe_allow_html=True,
    )

    try:
        cfg = _load_config()
        last_passing = load_last_passing_cycle()
        all_cycles   = load_recent_cycles(limit=30)
    except Exception as e:
        err_str = str(e).lower()
        if "relation" in err_str and "does not exist" in err_str or "no such table" in err_str:
            # Database connected but tables are missing (first run hasn't happened)
            cfg = _load_config()
            st.info(f"no cycles yet — first run is scheduled for {cfg.fetch_interval_hours} hours from initialization.")
            return
        else:
            logger.error(f"Hostile startup intercepted: {e}", exc_info=True)
            st.error("database not configured")
            return

    if not all_cycles:
        st.info(f"no cycles yet — first run is scheduled for {cfg.fetch_interval_hours} hours from initialization.")
        return

    # 1. Header strip — metric cards
    try:
        render_header(last_passing, all_cycles)
    except Exception as e:
        logger.error(f"render_header failed: {e}", exc_info=True)
        st.error("Header panel is temporarily unavailable.")

    st.divider()

    # 2. Activity log (main panel -- most vertical space)
    try:
        render_activity_log(all_cycles)
    except Exception as e:
        logger.error(f"render_activity_log failed: {e}", exc_info=True)
        st.error("Activity log panel is temporarily unavailable.")

    st.divider()

    # 3. Listings + gaps side by side
    col_left, col_right = st.columns(2)
    with col_left:
        try:
            render_listings_panel()
        except Exception as e:
            logger.error(f"render_listings_panel failed: {e}", exc_info=True)
            st.error("Listings panel is temporarily unavailable.")
    with col_right:
        try:
            render_gaps_panel()
        except Exception as e:
            logger.error(f"render_gaps_panel failed: {e}", exc_info=True)
            st.error("Gaps panel is temporarily unavailable.")

    st.divider()

    # 4. Ask your data
    try:
        render_ask_panel(_load_config())
    except Exception as e:
        logger.error(f"render_ask_panel failed: {e}", exc_info=True)
        st.error("Ask panel is temporarily unavailable.")

    # Footer
    last_cycle_str = fmt_ts(last_passing["started_at"]) if last_passing else "never"
    st.markdown(
        f'<div class="dash-footer">'
        f'Data panels reflect the last verified cycle only (rule 38) · '
        f'Activity log shows all cycles including failures · '
        f'No write operations are performed by this dashboard<br>'
        f'Last successful cycle: {{last_cycle_str}} · '
        f'<a href="https://github.com/user/edgedash" target="_blank">View on GitHub</a>'
        f'</div>',
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
