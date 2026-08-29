"""
Janitor agent — cleans up old listings.
"""

import logging
from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
import edgedash.storage as storage

logger = logging.getLogger(__name__)

class Janitor(Agent):
    """Deletes listings older than max_data_age_days."""

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        """Execute the cleanup."""
        logger.info("Janitor: deleting listings older than %d days", config.max_data_age_days)
        
        deleted = storage.delete_old_listings(config.max_data_age_days)
        
        logger.info("Janitor: deleted %d old listings", deleted)
        
        return AgentResult(
            agent="janitor",
            status="ok",
            records_touched=deleted,
            notes=f"Deleted {deleted} old listings."
        )
