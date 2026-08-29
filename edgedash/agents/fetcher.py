"""
Fetcher — real network agent.

Iterates over config.sources in order, calls each source's fetch(config),
combines all rows, computes stable listing ids, and writes to storage.

Per-source error isolation (steering rule 12):
- A failing source logs status="failed" to cycle_log, prints a warning,
  and is skipped. The cycle continues with the remaining sources.

stop_conditions respected (rule 29):
- max_pages  : passed to each source via config override; limits pagination.
- max_items  : caps total new listings written across all sources this cycle.
  Once the cap is reached, remaining sources are skipped with a log line.

notes field format (for AgentResult and cycle_log):
    "arbeitnow: 47 rows (12 new) | apify: FAILED (timeout)"
    "arbeitnow: 47 rows (12 new) | cap reached after 200 items"
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.sources.base import SOURCES
from edgedash.sources.http import SourceError

# Trigger registration of all built-in sources
import edgedash.sources  # noqa: F401

logger = logging.getLogger(__name__)


class Fetcher:
    name: str = "Fetcher"

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        fetched_at = datetime.now(timezone.utc).isoformat()
        summary_parts: list[str] = []
        total_new = 0

        # max_pages: override config by patching a temporary Config copy so the
        # Source interface (which only accepts config) remains unchanged.
        # max_items: enforced here in the Fetcher after each source completes.
        max_pages = stop_conditions.max_pages
        max_items = stop_conditions.max_items  # None means unlimited

        # Build a config with the page cap applied if one was given.
        # ArbeitnowSource and future sources read page limits from config
        # via the _PAGE_HARD_CAP constant, so we don't touch that — instead
        # we pass fetch_max_pages through config so sources may honour it.
        fetch_config = (
            dataclasses.replace(config, fetch_max_pages=max_pages)
            if max_pages is not None
            else config
        )

        for source_name in config.sources:
            # Stop early if item cap already reached
            if max_items is not None and total_new >= max_items:
                msg = f"cap reached after {total_new} new items — {source_name} skipped"
                logger.info("Fetcher: %s", msg)
                summary_parts.append(msg)
                break

            # ── resolve source from registry ──────────────────────────────
            source_cls = SOURCES.get(source_name)
            if source_cls is None:
                msg = f"unknown source '{source_name}' — not in SOURCES registry"
                logger.error("Fetcher: %s", msg)
                summary_parts.append(f"{source_name}: FAILED ({msg})")
                storage.log_cycle(
                    agent=f"Fetcher/{source_name}",
                    started_at=fetched_at,
                    finished_at=storage.now_utc(),
                    records_touched=0,
                    status="failed",
                    notes=msg,
                )
                continue

            source = source_cls()
            started_at = storage.now_utc()

            # ── fetch — isolated per source (rule 12) ─────────────────────
            try:
                raw_rows: list[dict] = source.fetch(fetch_config)
            except Exception as exc:
                short = type(exc).__name__ if not str(exc) else str(exc)[:120]
                warning = f"  ⚠  {source_name}: FAILED — {short}"
                print(warning)
                logger.error(
                    "Fetcher: source '%s' raised %s: %s",
                    source_name, type(exc).__name__, exc,
                )
                summary_parts.append(f"{source_name}: FAILED ({type(exc).__name__})")
                storage.log_cycle(
                    agent=f"Fetcher/{source_name}",
                    started_at=started_at,
                    finished_at=storage.now_utc(),
                    records_touched=0,
                    status="failed",
                    notes=str(exc)[:500],
                )
                continue

            # ── apply item cap to this source's rows ───────────────────────
            if max_items is not None:
                remaining = max_items - total_new
                raw_rows = raw_rows[:remaining]

            # ── build storage rows ─────────────────────────────────────────
            storage_rows: list[dict] = []
            for r in raw_rows:
                url = r.get("url") or ""
                src = r.get("source") or source_name
                storage_rows.append({
                    "id":          storage.stable_id(src, url),
                    "title":       r.get("title") or "",
                    "company":     r.get("company") or "",
                    "location":    r.get("location"),
                    "url":         url,
                    "description": r.get("description"),
                    "source":      src,
                    "posted_at":   r.get("posted_at"),
                    "fetched_at":  fetched_at,
                    "fit_score":   None,
                    "fit_reason":  None,
                })

            # ── write to storage ───────────────────────────────────────────
            new_count = storage.upsert_listings(storage_rows)
            total_new += new_count

            summary_parts.append(
                f"{source_name}: {len(storage_rows)} rows ({new_count} new)"
            )
            print(f"    • {source_name:<12} {len(storage_rows)} fetched ({new_count} new)")
            storage.log_cycle(
                agent=f"Fetcher/{source_name}",
                started_at=started_at,
                finished_at=storage.now_utc(),
                records_touched=new_count,
                status="ok",
                notes=f"{len(storage_rows)} fetched, {new_count} new",
            )
            logger.info(
                "Fetcher: %s — %d fetched, %d new",
                source_name, len(storage_rows), new_count,
            )

        notes = " | ".join(summary_parts) if summary_parts else "no sources configured"

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=total_new,
            notes=notes,
        )
