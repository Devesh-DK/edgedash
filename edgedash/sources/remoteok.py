"""
RemoteOKSource — free public remote tech jobs API, no key required.

API: https://remoteok.com/api
Response shape:
    JSON array where first element is legal notice metadata dict,
    and subsequent elements are job dicts with:
    id, slug, epoch, date, company, position, tags, description, location, url

Normalised output keys:
    source, external_id, title, company, location, url,
    description, posted_at, raw
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_json

logger = logging.getLogger(__name__)

_API_URL: str = "https://remoteok.com/api"


def _epoch_to_iso(epoch: int | float | None) -> str | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Return True if any keyword appears in the title, tags, or description."""
    if not keywords:
        return True
    tags = " ".join(job.get("tags") or [])
    haystack = (
        (job.get("position") or "").lower()
        + " "
        + tags.lower()
        + " "
        + (job.get("description") or "").lower()
    )
    return any(kw.lower() in haystack for kw in keywords)


def _matches_location(job: dict, city: str) -> bool:
    if not city or city.lower() in ("remote", "anywhere", "worldwide"):
        return True
    location = (job.get("location") or "").lower()
    return city.lower() in location or "worldwide" in location or "remote" in location


def _normalise(job: dict, source_name: str) -> dict:
    """Map a raw RemoteOK job dict to the EdgeDash normalised schema."""
    posted_at = job.get("date")
    if not posted_at and job.get("epoch"):
        posted_at = _epoch_to_iso(job.get("epoch"))

    ext_id = str(job.get("id")) if job.get("id") is not None else (job.get("slug") or None)

    return {
        "source": source_name,
        "external_id": ext_id,
        "title": job.get("position") or job.get("title") or None,
        "company": job.get("company") or None,
        "location": job.get("location") or "Worldwide",
        "url": job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at": posted_at,
        "raw": job,
    }


@register
class RemoteOKSource:
    name: str = "remoteok"

    def fetch(self, config: Config) -> list[dict]:
        """Fetch and filter RemoteOK listings; return normalised rows."""
        logger.info("RemoteOKSource: fetching from %s", _API_URL)

        try:
            raw_data = get_json(_API_URL)
        except SourceError as exc:
            logger.error("RemoteOKSource failed: %s", exc)
            return []

        if not isinstance(raw_data, list):
            logger.warning("RemoteOKSource: expected JSON list, got %s", type(raw_data).__name__)
            return []

        # First item in RemoteOK is typically legal notice dict with "legal" or "disclaimer"
        jobs: list[dict] = []
        for item in raw_data:
            if isinstance(item, dict) and ("position" in item or "title" in item or "slug" in item):
                jobs.append(item)

        logger.info("RemoteOKSource: received %d raw job listings", len(jobs))

        # Keyword filtering
        keyword_matches = [j for j in jobs if _matches_keywords(j, config.keywords)]
        logger.info(
            "RemoteOKSource: %d total raw -> %d keyword-matched",
            len(jobs),
            len(keyword_matches),
        )

        # Location filtering
        final_jobs = [j for j in keyword_matches if _matches_location(j, config.target_city)]
        if not final_jobs and keyword_matches:
            # Fall back to all keyword matches if location was overly restrictive
            logger.info("RemoteOKSource: location filter matched 0, falling back to all %d remote keyword matches", len(keyword_matches))
            final_jobs = keyword_matches

        return [_normalise(j, self.name) for j in final_jobs]
