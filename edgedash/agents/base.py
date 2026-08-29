"""
Base types shared by every EdgeDash agent.

All agents must satisfy the Agent protocol:
  - a `name` class attribute (str)
  - a `run(config, stop_conditions) -> AgentResult` method

AgentResult is the uniform return type logged by the Orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from edgedash.config import Config
from edgedash.planning import StopConditions


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    agent: str
    status: Literal["ok", "failed", "suspect"]
    records_touched: int
    notes: str = ""


# ---------------------------------------------------------------------------
# Protocol — structural typing, no ABC inheritance required
# ---------------------------------------------------------------------------

@runtime_checkable
class Agent(Protocol):
    """Every agent must expose name and run().

    stop_conditions carries the Orchestrator-set limits (rule 29).
    Agents must never decide their own limits — they read from stop_conditions
    and fall back to config defaults only when a field is None.
    """

    name: str

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        ...
