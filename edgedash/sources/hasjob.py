"""
HasjobSource — India's premier tech startup & engineer job feed by Hasgeek.

Feed: https://hasjob.co/feed (Atom XML feed covering Bengaluru, Mumbai, Delhi, Remote India)
No API key required.

Normalised output keys:
    source, external_id, title, company, location, url,
    description, posted_at, raw
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_text

logger = logging.getLogger(__name__)

_FEED_URL: str = "https://hasjob.co/feed"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _clean_html(raw_html: str) -> str:
    """Strip basic HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    return " ".join(text.split())


def _extract_company_from_url(url: str) -> str:
    """Extract company slug from https://hasjob.co/<company>/<job_id>."""
    try:
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        if path_parts:
            return path_parts[0].replace(".com", "").replace(".ai", "").replace(".in", "").replace("-", " ").title()
    except Exception:
        pass
    return "Hasjob Employer"


def _matches_keywords(job: dict, keywords: list[str]) -> bool:
    """Return True if any keyword appears in title, description, or location."""
    if not keywords:
        return True
    haystack = (
        (job.get("title") or "").lower()
        + " "
        + (job.get("company") or "").lower()
        + " "
        + (job.get("description") or "").lower()
    )
    return any(kw.lower() in haystack for kw in keywords)


def _matches_location(job: dict, city: str) -> bool:
    if not city or city.lower() in ("remote", "anywhere", "worldwide"):
        return True
    location = (job.get("location") or "").lower()
    city_lower = city.lower()
    return (
        city_lower in location
        or "anywhere" in location
        or "worldwide" in location
        or "remote" in location
        or "india" in location
    )


def _normalise(job: dict, source_name: str) -> dict:
    """Map parsed Hasjob dict to the EdgeDash normalised schema."""
    return {
        "source": source_name,
        "external_id": job.get("external_id"),
        "title": job.get("title") or None,
        "company": job.get("company") or "Hasjob Employer",
        "location": job.get("location") or "India",
        "url": job.get("url") or None,
        "description": job.get("description") or None,
        "posted_at": job.get("posted_at") or None,
        "raw": job,
    }


@register
class HasjobSource:
    name: str = "hasjob"

    def fetch(self, config: Config) -> list[dict]:
        """Fetch and filter Hasjob listings; return normalised rows."""
        logger.info("HasjobSource: fetching from %s", _FEED_URL)

        try:
            xml_text = get_text(_FEED_URL)
        except SourceError as exc:
            logger.error("HasjobSource failed: %s", exc)
            return []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("HasjobSource XML parse error: %s", exc)
            return []

        entries = root.findall("atom:entry", _ATOM_NS)
        logger.info("HasjobSource: received %d raw entries from Atom feed", len(entries))

        raw_jobs: list[dict] = []
        for e in entries:
            title = (e.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip()
            entry_id = (e.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
            link_el = e.find("atom:link", _ATOM_NS)
            url = link_el.get("href") if link_el is not None else entry_id

            location = (e.findtext("atom:location", default="", namespaces=_ATOM_NS) or "").strip() or "India"
            content_html = e.findtext("atom:content", default="", namespaces=_ATOM_NS) or ""
            summary_html = e.findtext("atom:summary", default="", namespaces=_ATOM_NS) or ""
            description = _clean_html(content_html or summary_html)

            posted_at = (
                e.findtext("atom:published", default="", namespaces=_ATOM_NS)
                or e.findtext("atom:updated", default="", namespaces=_ATOM_NS)
                or None
            )

            company = _extract_company_from_url(url)
            raw_id = entry_id.split("/")[-1] if "/" in entry_id else entry_id

            raw_jobs.append({
                "external_id": raw_id,
                "title": title,
                "company": company,
                "location": location,
                "url": url,
                "description": description,
                "posted_at": posted_at,
            })

        # Keyword filtering
        keyword_matches = [j for j in raw_jobs if _matches_keywords(j, config.keywords)]
        logger.info(
            "HasjobSource: %d total raw -> %d keyword-matched",
            len(raw_jobs),
            len(keyword_matches),
        )

        # Location filtering
        final_jobs = [j for j in keyword_matches if _matches_location(j, config.target_city)]
        if not final_jobs and keyword_matches:
            logger.info(
                "HasjobSource: location filter '%s' left 0, falling back to all %d keyword-matched jobs",
                config.target_city,
                len(keyword_matches),
            )
            final_jobs = keyword_matches

        return [_normalise(j, self.name) for j in final_jobs]
