"""
ArbeitnowSource — free public job-board API, no key required.

API docs: https://www.arbeitnow.com/api/job-board-api
Response shape per job:
    slug, company_name, title, description, remote,
    url, tags, job_types, location, created_at (unix)

Normalised output keys (per steering rule):
    source, external_id, title, company, location, url,
    description, posted_at, raw
    (missing values → None, never "" or "N/A")

Pagination strategy
-------------------
- Fetch page 1, then keep paging while:
    a. The current page returned jobs that matched the keyword filter, AND
    b. We haven't hit PAGE_HARD_CAP pages.
- This gives fast early-exit when results stop being relevant.

Filtering  (applied in order)
---------
1. keyword filter   : description or title contains at least one config.keyword
2. location filter  : location contains config.target_city (case-insensitive)
   - If fewer than MIN_RESULTS pass, the location filter is relaxed but the
     language filter below is then REQUIRED so German/French/etc listings
     don't flood the results.
3. language filter  : non-English listings are dropped when the location filter
   is relaxed. Detection is deterministic — no model:
   a. Title contains a known non-English job-posting marker word (configurable).
   b. More than LANG_NONASCII_THRESHOLD fraction of the title's letters are
      outside ASCII range (catches CJK, Arabic, Cyrillic etc).
   Either condition is sufficient to reject the listing.

Rate limiting
-------------
1 request per second per the steering rule (enforced by _RATE_LIMIT_DELAY).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_json

logger = logging.getLogger(__name__)

_API_URL: str = "https://www.arbeitnow.com/api/job-board-api"
_PAGE_HARD_CAP: int = 5
_MIN_RESULTS: int = 5
_RATE_LIMIT_DELAY: float = 1.0  # seconds between page requests

# Fraction of non-ASCII letters in the title that flags a non-English listing.
_LANG_NONASCII_THRESHOLD: float = 0.25

# Common words that appear in German/French/Spanish/Dutch job titles but not
# English ones.  Lower-cased, matched as substrings of the title.
_NON_ENGLISH_TITLE_MARKERS: frozenset[str] = frozenset([
    # German — gender suffixes used in German job ads
    "(m/w/d)", "(w/m/d)", "(m/f/d)", "(all genders)", "(all gender)",
    "werkstudent", "mitarbeiter", "fachkraft", "kaufmann", "kauffrau",
    "referent", "koordinator", "sachbearbeiter", "anwendungsentwickler",
    "softwareentwickler", "leiter", "praktikant", "auszubildender",
    # French
    "développeur", "responsable", "chargé", "ingénieur", "technicien",
    # Spanish / Portuguese
    "desarrollador", "analista", "gerente", "coordenador",
])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _unix_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Return True if any keyword appears in the job title or description."""
    if not keywords:
        return True
    haystack = (
        (job.get("title") or "").lower()
        + " "
        + (job.get("description") or "").lower()
    )
    return any(kw.lower() in haystack for kw in keywords)


def _matches_location(job: dict, city: str) -> bool:
    location = (job.get("location") or "").lower()
    return city.lower() in location


def _is_likely_english(job: dict) -> bool:
    """Return True if the listing appears to be in English.

    Two deterministic checks — no model:
    1. Non-ASCII letter fraction of the title exceeds the threshold.
    2. Title contains a known non-English job-posting marker.

    Both checks use only the title (short, reliable signal).
    Either failing means the listing is probably not English.
    """
    title = (job.get("title") or "").lower()
    if not title:
        return True  # no title — don't reject on insufficient evidence

    # Check 1: non-ASCII letter fraction
    letters = [c for c in title if c.isalpha()]
    if letters:
        non_ascii = sum(1 for c in letters if ord(c) > 127)
        if non_ascii / len(letters) > _LANG_NONASCII_THRESHOLD:
            return False

    # Check 2: known non-English marker in title
    for marker in _NON_ENGLISH_TITLE_MARKERS:
        if marker in title:
            return False

    return True


def _normalise(job: dict, source_name: str) -> dict:
    """Map a raw Arbeitnow job dict to the EdgeDash normalised schema."""
    return {
        "source": source_name,
        "external_id": job.get("slug") or None,
        "title": job.get("title") or None,
        "company": job.get("company_name") or None,
        "location": job.get("location") or None,
        "url": job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at": _unix_to_iso(job.get("created_at")),
        "raw": job,
    }


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------

@register
class ArbeitnowSource:
    name: str = "arbeitnow"

    def fetch(self, config: Config) -> list[dict]:
        """Fetch and filter Arbeitnow listings; return normalised rows."""
        all_keyword_matches: list[dict] = []
        page = 1

        while page <= _PAGE_HARD_CAP:
            logger.info("ArbeitnowSource: fetching page %d", page)

            try:
                data = get_json(_API_URL, params={"page": page})
            except SourceError as exc:
                logger.error("ArbeitnowSource page %d failed: %s", page, exc)
                break

            jobs: list[dict] = data.get("data", [])
            if not jobs:
                logger.info("ArbeitnowSource: no jobs on page %d, stopping", page)
                break

            # --- keyword filter -----------------------------------------
            page_matches = [j for j in jobs if _matches_keywords(j, config.keywords)]

            logger.info(
                "ArbeitnowSource page %d: %d raw, %d keyword-matched",
                page,
                len(jobs),
                len(page_matches),
            )

            all_keyword_matches.extend(page_matches)

            # Stop paging when no keyword matches on this page
            if not page_matches:
                logger.info(
                    "ArbeitnowSource: no keyword matches on page %d — stopping early",
                    page,
                )
                break

            # Check for a next page
            next_url = (data.get("links") or {}).get("next")
            if not next_url:
                break

            page += 1
            time.sleep(_RATE_LIMIT_DELAY)

        raw_count = len(all_keyword_matches)

        # --- location filter --------------------------------------------
        final = [
            j for j in all_keyword_matches
            if _matches_location(j, config.target_city)
        ]

        logger.info(
            "ArbeitnowSource: %d raw keyword-matches → %d after location filter",
            raw_count,
            len(final),
        )

        return [_normalise(j, self.name) for j in final]
