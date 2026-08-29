"""
JobicySource — free public remote jobs API, no key required.

API: https://jobicy.com/api/v2/remote-jobs
Response shape:
    JSON object with "jobs" key containing a list of job dicts:
    id, url, jobTitle, companyName, jobGeo, jobDescription, pubDate, jobCategories

Normalised output keys:
    source, external_id, title, company, location, url,
    description, posted_at, raw
"""

from __future__ import annotations

import logging
from typing import Any

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_json

logger = logging.getLogger(__name__)

_API_URL: str = "https://jobicy.com/api/v2/remote-jobs"


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Return True if any keyword appears in title, categories, or description."""
    if not keywords:
        return True
    categories = job.get("jobCategories")
    cat_str = " ".join(categories) if isinstance(categories, list) else str(categories or "")
    haystack = (
        (job.get("jobTitle") or "").lower()
        + " "
        + cat_str.lower()
        + " "
        + (job.get("jobDescription") or "").lower()
    )
    return any(kw.lower() in haystack for kw in keywords)


def _matches_location(job: dict, city: str) -> bool:
    if not city or city.lower() in ("remote", "anywhere", "worldwide"):
        return True
    geo = (job.get("jobGeo") or "").lower()
    return city.lower() in geo or "anywhere" in geo or "worldwide" in geo or "remote" in geo


def _normalise(job: dict, source_name: str) -> dict:
    """Map a raw Jobicy job dict to the EdgeDash normalised schema."""
    ext_id = str(job.get("id")) if job.get("id") is not None else None
    return {
        "source": source_name,
        "external_id": ext_id,
        "title": job.get("jobTitle") or None,
        "company": job.get("companyName") or None,
        "location": job.get("jobGeo") or "Remote",
        "url": job.get("url") or None,
        "description": job.get("jobDescription") or None,
        "posted_at": job.get("pubDate") or None,
        "raw": job,
    }


@register
class JobicySource:
    name: str = "jobicy"

    def fetch(self, config: Config) -> list[dict]:
        """Fetch and filter Jobicy listings; return normalised rows."""
        logger.info("JobicySource: fetching from %s", _API_URL)

        try:
            data: dict[str, Any] = get_json(_API_URL, params={"count": 50})
        except SourceError as exc:
            logger.error("JobicySource failed: %s", exc)
            return []

        jobs: list[dict] = data.get("jobs", []) if isinstance(data, dict) else []
        logger.info("JobicySource: received %d raw job listings", len(jobs))

        if not jobs:
            return []

        # Keyword filtering
        keyword_matches = [j for j in jobs if _matches_keywords(j, config.keywords)]
        logger.info(
            "JobicySource: %d total raw -> %d keyword-matched",
            len(jobs),
            len(keyword_matches),
        )

        # Location filtering
        final_jobs = [j for j in keyword_matches if _matches_location(j, config.target_city)]
        if not final_jobs and keyword_matches:
            logger.info("JobicySource: location filter matched 0, falling back to all %d remote keyword matches", len(keyword_matches))
            final_jobs = keyword_matches

        return [_normalise(j, self.name) for j in final_jobs]
