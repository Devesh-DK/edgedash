"""
edgedash/scoring.py — deterministic, pure-function scorer.

No model calls. No network. No imports from llm.py.
All scoring arithmetic lives here; the model only feeds *facts* in.

Public API
----------
    score_listing(listing, facts, config) -> dict
        {"score": int 0-100, "reason": str, "components": {...}}

    build_reason(components, facts, config) -> str
        Compact human-readable string built from numbers, never from model text.

Seniority band order (used for seniority_fit distance):
    junior -> mid -> senior -> lead
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ordered band list — index difference gives "distance" for seniority_fit.
_SENIORITY_BANDS: list[str] = ["junior", "mid", "senior", "lead"]

# Seniority fit score by band distance.
_SENIORITY_BY_DISTANCE: dict[int, float] = {0: 1.0, 1: 0.6, 2: 0.25}
# distance >= 3 -> 0.0

# Recency: linear decay over this many days to reach 0.0.
_RECENCY_DECAY_DAYS: int = 30

# Nice-to-have skills count at this fraction of a required skill.
_NICE_WEIGHT: float = 1 / 3


# ---------------------------------------------------------------------------
# Component scorers — each returns a float in [0.0, 1.0]
# ---------------------------------------------------------------------------

def _score_skill_match(facts: dict, config: Config) -> tuple[float, list[str]]:
    """Return (normalised_score, missing_required_skills).

    Fraction of required skills the candidate has, with nice-to-have skills
    contributing at 1/3 weight each.

    Empty required_skills: returns 1.0 with no gaps (no denominator to divide by).
    Matching is case-insensitive; the extractor already lowercases skill lists,
    and we lowercase config.my_skills here for safety.
    """
    my_skills = {s.lower() for s in config.my_skills}

    required: list[str] = facts.get("required_skills") or []
    nice: list[str] = facts.get("nice_to_have") or []

    if not required:
        # No required skills stated — full marks, no gap to report.
        return 1.0, []

    # Required skill hits
    req_hits = sum(1 for s in required if s.lower() in my_skills)
    req_misses = [s for s in required if s.lower() not in my_skills]

    # Nice-to-have hits (only count up to the denominator headroom)
    nice_hits = sum(1 for s in nice if s.lower() in my_skills)

    # Denominator: each required skill = 1 slot, each nice-to-have = 1/3 slot.
    denominator = len(required) + len(nice) * _NICE_WEIGHT
    numerator = req_hits + nice_hits * _NICE_WEIGHT

    score = numerator / denominator if denominator > 0 else 1.0
    return min(score, 1.0), req_misses


def _score_seniority_fit(facts: dict, config: Config) -> float:
    """Distance-based fit on the ordered junior->mid->senior->lead band scale.

    unknown seniority in facts -> 0.5 (neutral, we cannot tell).
    unknown target_seniority in config -> also 0.5.
    """
    listing_level: str = (facts.get("seniority") or "unknown").lower()
    target_level: str = (config.target_seniority or "unknown").lower()

    if listing_level == "unknown" or target_level == "unknown":
        return 0.5

    try:
        li = _SENIORITY_BANDS.index(listing_level)
        ti = _SENIORITY_BANDS.index(target_level)
    except ValueError:
        # Band not in our list (shouldn't happen after extractor normalisation,
        # but be safe rather than crash).
        return 0.5

    distance = abs(li - ti)
    return _SENIORITY_BY_DISTANCE.get(distance, 0.0)


def _score_location_fit(listing: dict, facts: dict, config: Config) -> float:
    """Remote > city match > unknown > clearly elsewhere.

    Decision tree:
      1. remote_ok is True  -> 1.0
      2. listing.location contains target_city (case-insensitive) -> 1.0
      3. remote_ok is None AND location is None/empty -> 0.5 (unknown)
      4. otherwise -> 0.1 (on-site, different location)
    """
    remote_ok: bool | None = facts.get("remote_ok")

    if remote_ok is True:
        return 1.0

    location: str = (listing.get("location") or "").lower()
    target_city: str = (config.target_city or "").lower()

    if target_city and target_city in location:
        return 1.0

    if remote_ok is None and not location:
        return 0.5

    return 0.1


def _score_recency(listing: dict) -> float:
    """Linear decay from 1.0 today to 0.0 at 30 days old.

    posted_at is an ISO-8601 string or None.
    None -> 0.5 (neutral; we cannot penalise what we cannot read).
    Negative age (future date) -> clamped to 1.0.
    """
    posted_at_raw: str | None = listing.get("posted_at")

    if not posted_at_raw:
        return 0.5

    try:
        posted_dt = datetime.fromisoformat(posted_at_raw)
        # Make naive datetimes UTC-aware for safe arithmetic.
        if posted_dt.tzinfo is None:
            posted_dt = posted_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_days = (now - posted_dt).total_seconds() / 86_400
    except (ValueError, TypeError):
        # Unparseable date — treat as unknown.
        return 0.5

    if age_days < 0:
        return 1.0
    return max(0.0, 1.0 - age_days / _RECENCY_DECAY_DAYS)


# ---------------------------------------------------------------------------
# Public scoring function
# ---------------------------------------------------------------------------

def score_listing(listing: dict, facts: dict, config: Config) -> dict[str, Any]:
    """Score one listing against the user profile. Pure function, no I/O.

    Args:
        listing: A storage listing row (needs at least 'location', 'posted_at').
        facts:   Normalised extraction dict from edgedash/agents/extractor.py.
        config:  Loaded Config instance carrying profile + weights.

    Returns:
        {
            "score": int,          # 0-100 weighted composite
            "reason": str,         # human-readable summary (rule 19)
            "components": {
                "skill_match":    float,  # 0.0-1.0
                "seniority_fit":  float,
                "location_fit":   float,
                "recency":        float,
            }
        }
    """
    skill_raw, missing_skills = _score_skill_match(facts, config)
    seniority_raw  = _score_seniority_fit(facts, config)
    location_raw   = _score_location_fit(listing, facts, config)
    recency_raw    = _score_recency(listing)

    components = {
        "skill_match":   round(skill_raw, 4),
        "seniority_fit": round(seniority_raw, 4),
        "location_fit":  round(location_raw, 4),
        "recency":       round(recency_raw, 4),
    }

    weighted = (
        skill_raw    * config.weight_skill_match
        + seniority_raw * config.weight_seniority_fit
        + location_raw  * config.weight_location_fit
        + recency_raw   * config.weight_recency
    )

    # Clamp to [0, 100] and round to nearest int.
    score = max(0, min(100, round(weighted * 100)))

    reason = build_reason(components, facts, config, missing_skills)

    return {"score": score, "reason": reason, "components": components}


# ---------------------------------------------------------------------------
# Reason builder (rule 19 — code-generated, never free model text)
# ---------------------------------------------------------------------------

def build_reason(
    components: dict,
    facts: dict,
    config: Config,
    missing_skills: list[str] | None = None,
) -> str:
    """Assemble a compact reason string from score component values.

    Style: "4/6 required skills · seniority fits · remote · posted 2d ago · gap: kubernetes, spark"

    Args:
        components:     The components dict from score_listing().
        facts:          The normalised extraction dict.
        config:         Config (for target_seniority, target_city).
        missing_skills: Required skills the candidate is missing. If None,
                        recomputed from facts + config (so this function can
                        be called standalone).
    """
    parts: list[str] = []

    # ── skill match ──────────────────────────────────────────────────────
    required: list[str] = facts.get("required_skills") or []
    if not required:
        parts.append("no required skills listed")
    else:
        my_skills = {s.lower() for s in config.my_skills}
        hits = sum(1 for s in required if s.lower() in my_skills)
        parts.append(f"{hits}/{len(required)} required skills")

    # ── seniority ────────────────────────────────────────────────────────
    fit = components["seniority_fit"]
    listing_level = (facts.get("seniority") or "unknown").lower()
    if listing_level == "unknown":
        parts.append("seniority unknown")
    elif fit >= 1.0:
        parts.append("seniority fits")
    elif fit >= 0.6:
        parts.append(f"seniority close ({listing_level})")
    else:
        parts.append(f"seniority mismatch ({listing_level})")

    # ── location ─────────────────────────────────────────────────────────
    loc_score = components["location_fit"]
    remote_ok = facts.get("remote_ok")
    if remote_ok is True:
        parts.append("remote")
    elif loc_score >= 1.0:
        parts.append(f"in {config.target_city}")
    elif loc_score >= 0.5:
        parts.append("location unknown")
    else:
        parts.append("on-site elsewhere")

    # ── recency ──────────────────────────────────────────────────────────
    rec = components["recency"]
    if rec == 0.5:
        parts.append("posted date unknown")
    else:
        # Back-calculate age in days from the decay formula.
        age_days = round((1.0 - rec) * _RECENCY_DECAY_DAYS)
        if age_days == 0:
            parts.append("posted today")
        elif age_days == 1:
            parts.append("posted 1d ago")
        else:
            parts.append(f"posted {age_days}d ago")

    # ── skill gaps ───────────────────────────────────────────────────────
    if missing_skills is None:
        _, missing_skills = _score_skill_match(facts, config)

    if missing_skills:
        gap_str = ", ".join(missing_skills[:5])  # cap at 5 to keep line short
        if len(missing_skills) > 5:
            gap_str += f" (+{len(missing_skills) - 5} more)"
        parts.append(f"gap: {gap_str}")

    return " · ".join(parts)
