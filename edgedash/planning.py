"""
edgedash/planning.py — pure, deterministic plan construction.

Public API
----------
build_plan(state, config) -> Plan

Design notes
------------
- build_plan is a pure function: no I/O, no DB, no network, no LLM.
- Skipped agents appear explicitly in the Plan with a reason (rule 31).
- Stop conditions for every Task come from config — never from the agent
  itself (rule 29).
- The reason strings name the state value that drove the decision so the
  Orchestrator can log them verbatim (rule 31).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from edgedash.config import Config
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StopConditions:
    """Hard limits the Orchestrator passes to a sub-agent (rule 29)."""
    max_items: int | None = None       # listings / skills to process
    max_pages: int | None = None       # fetch pages per source
    max_seconds: int | None = None     # wall-clock budget


@dataclass(frozen=True)
class Task:
    """One entry in the ordered Plan.

    Attributes
    ----------
    agent_name:    Identifier matching the agent registry key.
    goal:          Human-readable statement of what the agent must do.
    stop_conditions: Hard limits sourced from config (never agent-decided).
    reason:        The state value or threshold that caused this decision.
    skipped:       True when the agent is included only to document why it
                   was NOT scheduled — it must not be executed.
    """

    agent_name: str
    goal: str
    stop_conditions: StopConditions
    reason: str
    skipped: bool = False


@dataclass
class Plan:
    """Ordered list of Tasks produced by build_plan.

    Tasks appear in pipeline order: fetch → score → analyse.
    Skipped tasks are included so the Orchestrator can log them (rule 31).
    """

    tasks: list[Task] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    def runnable(self) -> list[Task]:
        """Tasks that should actually execute (skipped=False)."""
        return [t for t in self.tasks if not t.skipped]

    def skipped_tasks(self) -> list[Task]:
        """Tasks that will be skipped this cycle."""
        return [t for t in self.tasks if t.skipped]

    # ------------------------------------------------------------------
    # Rendering (rule 31)
    # ------------------------------------------------------------------

    def render(self) -> str:
        """Return a compact, human-readable plan — one line per agent.

        Format per line:
            [RUN]  agent_name | goal | stop: ... | reason
            [SKIP] agent_name | reason
        """
        lines: list[str] = ["=== CYCLE PLAN ==="]
        for task in self.tasks:
            if task.skipped:
                lines.append(f"  [SKIP] {task.agent_name:<10}  {task.reason}")
            else:
                sc = task.stop_conditions
                parts: list[str] = []
                if sc.max_pages is not None:
                    parts.append(f"max_pages={sc.max_pages}")
                if sc.max_items is not None:
                    parts.append(f"max_items={sc.max_items}")
                if sc.max_seconds is not None:
                    parts.append(f"max_seconds={sc.max_seconds}")
                stop_str = ", ".join(parts) if parts else "none"
                lines.append(
                    f"  [RUN]  {task.agent_name:<10}  goal: {task.goal}"
                    f"  |  stop: {stop_str}"
                    f"  |  {task.reason}"
                )
        lines.append("==================")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------

def build_plan(state: SystemState, config: Config) -> Plan:
    """Construct an ordered Plan from system state and config thresholds.

    Pure function — no I/O of any kind.

    Decision rules
    --------------
    fetch   : run when hours_since_fetch is None (never fetched) OR
              hours_since_fetch >= config.fetch_interval_hours
    score   : run when unscored_count > 0
    analyse : run when gaps_stale is True (covers gaps_computed_at is None)

    Every agent appears in the returned Plan, skipped or not.
    """
    tasks: list[Task] = [
        _decide_fetch(state, config),
        _decide_score(state, config),
        _decide_analyse(state, config),
        _decide_janitor(state, config),
    ]
    return Plan(tasks=tasks)


# ---------------------------------------------------------------------------
# Per-agent decision helpers
# ---------------------------------------------------------------------------

def _decide_fetch(state: SystemState, config: Config) -> Task:
    """Decide whether to run the Fetcher."""
    if state.hours_since_fetch is None:
        run = True
        reason = "hours_since_fetch=None (never fetched)"
    elif state.hours_since_fetch >= config.fetch_interval_hours:
        run = True
        reason = (
            f"hours_since_fetch={state.hours_since_fetch:.1f} "
            f">= fetch_interval_hours={config.fetch_interval_hours}"
        )
    else:
        run = False
        reason = (
            f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} "
            f"< fetch_interval_hours={config.fetch_interval_hours}"
        )

    return Task(
        agent_name="fetcher",
        goal=f"Fetch new job listings from all enabled sources into storage",
        stop_conditions=StopConditions(
            max_pages=config.fetch_max_pages,
            max_items=config.fetch_max_listings,
        ),
        reason=reason,
        skipped=not run,
    )


def _decide_score(state: SystemState, config: Config) -> Task:
    """Decide whether to run the Scorer."""
    if state.unscored_count > 0:
        run = True
        reason = f"unscored_count={state.unscored_count}"
    else:
        run = False
        reason = f"skipped: unscored_count=0"

    return Task(
        agent_name="scorer",
        goal=(
            f"Score up to {config.scoring_batch_size} unscored listings "
            f"against the user profile"
        ),
        stop_conditions=StopConditions(
            max_items=config.scoring_batch_size,
            max_seconds=config.score_max_seconds,
        ),
        reason=reason,
        skipped=not run,
    )


def _decide_analyse(state: SystemState, config: Config) -> Task:
    """Decide whether to run the GapAnalyzer."""
    if state.gaps_stale:
        if state.gaps_computed_at is None:
            reason = "gaps_computed_at=None (never analysed)"
        else:
            reason = (
                f"gaps_stale=True (scores newer than "
                f"gaps_computed_at={state.gaps_computed_at})"
            )
        run = True
    else:
        run = False
        reason = f"skipped: gaps_stale=False (snapshot is current)"

    return Task(
        agent_name="analyser",
        goal="Compute weighted skill-gap snapshot from all scored listings",
        stop_conditions=StopConditions(
            max_seconds=config.analyse_max_seconds,
        ),
        reason=reason,
        skipped=not run,
    )


def _decide_janitor(state: SystemState, config: Config) -> Task:
    """Decide whether to run the Janitor."""
    # The janitor can run unconditionally to ensure the database is kept clean
    # Alternatively, it could run only if a fetch happened, but running it
    # every cycle is fast and ensures we don't hold on to expired data.
    return Task(
        agent_name="janitor",
        goal=f"Delete listings older than {config.max_data_age_days} days",
        stop_conditions=StopConditions(),
        reason="unconditional background cleanup",
        skipped=False,
    )
