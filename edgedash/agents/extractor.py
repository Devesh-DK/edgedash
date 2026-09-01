"""
edgedash/agents/extractor.py — LLM-based fact extraction from job descriptions.

This is the ONLY part of the scoring pipeline that calls a model.
It extracts structured facts from a listing's description text and nothing
more. It knows nothing about a candidate, a profile, or scoring weights
(steering rule 16 — the model never sees those).

Public API
----------
    extract(listing, config) -> dict

    Returns a validated extraction dict with exactly these keys:
        required_skills  list[str]
        nice_to_have     list[str]
        seniority        "junior"|"mid"|"senior"|"lead"|"unknown"
        years_required   int | None
        remote_ok        bool | None

Cache behaviour (steering rule 18)
------------------------------------
    A SHA-256 hash of the description text is computed first.
    If the hash already exists in extraction_cache the stored result is
    returned immediately — no model call is made.
    On a miss the model is called, the result is validated, normalised,
    stored under the hash, and returned.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.llm import LLMError, complete_json

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction schema (steering rule 16 — no score field, ever)
# ---------------------------------------------------------------------------

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "required_skills": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Skills the listing explicitly requires.",
        },
        "nice_to_have": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Skills listed as preferred or a bonus, not required.",
        },
        "seniority": {
            "type": "string",
            "enum": ["junior", "mid", "senior", "lead", "manager", "unknown"],
            "description": "Seniority level stated by the listing.",
        },
        "years_required": {
            "type": ["integer", "null"],
            "description": "Minimum years of experience stated. null if not stated.",
        },
        "remote_ok": {
            "type": ["boolean", "null"],
            "description": (
                "true if remote is explicitly allowed, "
                "false if explicitly on-site only, "
                "null if the listing does not say."
            ),
        },
    },
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are reading a job listing document. Your only task is to extract \
specific facts that are explicitly stated in the text.

Rules you must follow:
- Extract ONLY what the listing explicitly states. Do not infer, guess, \
or assume anything that is not written down.
- If the listing does not state something, use null (for years_required \
and remote_ok) or an empty list (for skill lists).
- Do not evaluate any candidate. No candidate exists. \
You are reading a document, nothing more.
- Do not add a score, rating, ranking, or any evaluative field of any kind.

Extract the following fields:

required_skills  — skills the listing explicitly requires (e.g. "must have", \
"required", "you will need"). Empty list if none are stated.
nice_to_have     — skills the listing describes as preferred, a bonus, or \
"nice to have" but not required. Empty list if none are stated.
seniority        — one of: junior, mid, senior, lead, manager, unknown. \
Use "unknown" if the listing gives no clear signal.
years_required   — the minimum years of experience the listing states, \
as an integer. null if not stated. Never derive this from seniority.
remote_ok        — true if the listing explicitly says remote is allowed, \
false if it explicitly says on-site only, null if not stated.

Job listing:
----
{description}
----"""


def _build_prompt(description: str) -> str:
    return _PROMPT_TEMPLATE.format(description=description)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _description_hash(text: str) -> str:
    """Return a stable SHA-256 hex digest of the description text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(extracted: dict) -> dict:
    """Lowercase all skill strings so 'Postgres' and 'postgres' match later."""
    return {
        "required_skills": [s.lower() for s in extracted.get("required_skills") or []],
        "nice_to_have":    [s.lower() for s in extracted.get("nice_to_have") or []],
        "seniority":       extracted.get("seniority", "unknown"),
        "years_required":  extracted.get("years_required"),
        "remote_ok":       extracted.get("remote_ok"),
    }


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def extract(listing: dict, config: Config) -> dict:
    """Extract structured facts from a job listing's description.

    Checks the extraction_cache first (steering rule 18). Returns the cached
    result immediately on a hit. On a miss, calls the LLM, normalises the
    result, stores it in the cache, and returns it.

    The model is never asked to score, rank, or evaluate anything. It reads
    the description as a document and reports only what is written there.

    Args:
        listing: A listing dict containing at least a "description" key.
        config:  The loaded Config instance (passed through to complete_json).

    Returns:
        A normalised extraction dict matching EXTRACTION_SCHEMA.

    Raises:
        LLMError: If the model call fails after all retries (steering rule 17).
                  Callers are responsible for catching this per-listing so that
                  one failure does not stop the remaining listings.
    """
    description: str = listing.get("description") or ""
    listing_id: str = listing.get("id", "<unknown>")

    desc_hash = _description_hash(description)

    # Stamp the hash onto the listings row so the extraction_cache entry can
    # always be traced back to its source listing.  Safe to call every time
    # because the UPDATE is idempotent and the listing may not have an id
    # column when called outside the scoring loop (e.g. in tests).
    if listing_id != "<unknown>":
        storage.set_listing_hash(listing_id, desc_hash)

    # ── cache check ──────────────────────────────────────────────────────
    cached = storage.get_extraction(desc_hash)
    if cached is not None:
        logger.debug("extractor: cache HIT for listing %s (hash %s…)", listing_id, desc_hash[:12])
        return cached

    logger.info("extractor: cache MISS for listing %s — calling LLM", listing_id)

    # ── model call ───────────────────────────────────────────────────────
    if not description.strip():
        # No description to extract from — return safe empty result without
        # calling the model (a call on empty text wastes quota and returns
        # meaningless output).
        logger.warning("extractor: listing %s has no description, returning empty extraction", listing_id)
        empty: dict = {
            "required_skills": [],
            "nice_to_have": [],
            "seniority": "unknown",
            "years_required": None,
            "remote_ok": None,
        }
        storage.set_extraction(desc_hash, empty)
        return empty

    prompt = _build_prompt(description)

    # complete_json validates against EXTRACTION_SCHEMA before returning
    # and retries once on failure (steering rule 17). If it raises LLMError
    # after all retries, we let it propagate — callers handle it per-listing.
    raw = complete_json(prompt, EXTRACTION_SCHEMA, config=config)

    normalised = _normalise(raw)

    # ── cache store ──────────────────────────────────────────────────────
    storage.set_extraction(desc_hash, normalised)
    logger.info("extractor: stored extraction for listing %s", listing_id)

    return normalised
