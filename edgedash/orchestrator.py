"""
Orchestrator — state-driven cycle controller (rules 28-33).

Flow
----
1. init storage
2. read_state  → SystemState          (rule 28 — no fixed sequence)
3. build_plan  → Plan                 (rule 29 — limits from config)
4. apply_overrides if --force agents given (does not alter build_plan)
5. PRINT rendered plan before any execution (rule 31)
6. --dry-run exits here — no writes, no API calls
7. execute runnable tasks in plan order
   - each task wrapped in try/except  (rule 32 — one failure ≠ cycle failure)
   - skipped tasks logged with reason (rule 31)
8. write exactly ONE summary row to cycle_log (rule 33)
9. exit 0 for complete, partial, and nothing_to_do outcomes (rule 28)

Adding a fourth agent requires:
  a. A class with run(config, stop_conditions) -> AgentResult
  b. One entry in _build_registry mapping agent_name → instance
  c. One _decide_<name>() helper added to planning.build_plan
  Nothing else changes.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Literal

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.scorer import Scorer
from edgedash.agents.verifier import Verifier
from edgedash.agents.janitor import Janitor
from edgedash.config import Config
from edgedash.planning import Plan, StopConditions, Task, build_plan
from edgedash.state import SystemState, read_state

logger = logging.getLogger(__name__)

_W = 62  # console width

Outcome = Literal["complete", "partial", "nothing_to_do", "dry_run"]


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

def _build_registry(config: Config) -> dict[str, Agent]:
    """Return a name→agent map.

    Keys must match the agent_name strings used in planning.py.
    To add a new agent: one new entry here and one _decide_*() in planning.py.
    """
    fetcher: Agent = MockFetcher() if config.use_mock_fetcher else Fetcher()
    return {
        "fetcher":  fetcher,
        "scorer":   Scorer(),
        "analyser": GapAnalyzer(),
        "janitor":  Janitor(),
        "verifier": Verifier(),
    }


# ---------------------------------------------------------------------------
# Force override — does NOT alter build_plan (pure function stays pure)
# ---------------------------------------------------------------------------

def _apply_overrides(
    plan: Plan,
    forced_agents: list[str],
    config: Config,
) -> tuple[Plan, list[str]]:
    """Return a new Plan with forced agents un-skipped, plus a list of overrides applied.

    build_plan is not touched. This function constructs a replacement task
    for each forced agent, using the same stop_conditions and goal as the
    original task but with skipped=False and reason="forced by operator".

    Unknown agent names are logged and ignored.
    """
    if not forced_agents:
        return plan, []

    known = {t.agent_name for t in plan.tasks}
    applied: list[str] = []
    new_tasks: list[Task] = []

    for task in plan.tasks:
        if task.agent_name in forced_agents and task.skipped:
            new_tasks.append(Task(
                agent_name=task.agent_name,
                goal=task.goal,
                stop_conditions=task.stop_conditions,
                reason="forced by operator",
                skipped=False,
            ))
            applied.append(task.agent_name)
        else:
            new_tasks.append(task)

    # Warn about names that don't exist in the plan at all
    for name in forced_agents:
        if name not in known:
            logger.warning(
                "Orchestrator: --force '%s' is not a known agent — ignored. "
                "Known agents: %s",
                name, sorted(known),
            )
            print(f"  ⚠  --force '{name}' is not a known agent — ignored.")

    return Plan(tasks=new_tasks), applied


# ---------------------------------------------------------------------------
# --explain: state → decision trace
# ---------------------------------------------------------------------------

def _print_explain(state: SystemState, plan: Plan, config: Config) -> None:
    """Print each SystemState field alongside the decision it drove."""
    _section("State → Decision trace  (--explain)")

    def _line(field: str, value: object, drove: str) -> None:
        print(f"  {field:<28}  {str(value):<30}  → {drove}")

    # Map each state value to the task reason it produced
    task_reasons = {t.agent_name: t.reason for t in plan.tasks}

    _line(
        "last_fetch_at",
        state.last_fetch_at or "None",
        task_reasons.get("fetcher", "—"),
    )
    _line(
        "hours_since_fetch",
        f"{state.hours_since_fetch:.2f}h" if state.hours_since_fetch is not None else "None",
        f"threshold: fetch_interval_hours={config.fetch_interval_hours}",
    )
    _line(
        "unscored_count",
        state.unscored_count,
        task_reasons.get("scorer", "—"),
    )
    _line(
        "gaps_computed_at",
        state.gaps_computed_at or "None",
        task_reasons.get("analyser", "—"),
    )
    _line(
        "gaps_stale",
        state.gaps_stale,
        task_reasons.get("analyser", "—"),
    )
    _line(
        "last_cycle_verdict",
        state.last_cycle_verdict or "None",
        "informational only",
    )
    _line(
        "last_cycle_at",
        state.last_cycle_at or "None",
        "informational only",
    )


# ---------------------------------------------------------------------------
# Console formatting helpers
# ---------------------------------------------------------------------------

def _banner(text: str) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {text}")
    print(f"{'─' * _W}")


def _row(label: str, value: object) -> None:
    print(f"  {label:<30} {value}")


def _section(title: str) -> None:
    print(f"\n  ── {title}")


# ---------------------------------------------------------------------------
# Execution helpers
# ---------------------------------------------------------------------------

def _run_task(
    task: Task,
    registry: dict[str, Agent],
    config: Config,
) -> tuple[AgentResult, float]:
    """Resolve the agent, call run(), and return (result, elapsed_seconds).

    Raises if the agent_name is not in the registry (programming error).
    """
    agent = registry[task.agent_name]
    t0 = datetime.now(timezone.utc)
    result = agent.run(config, task.stop_conditions)
    elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
    return result, elapsed


# ---------------------------------------------------------------------------
# Summary row builder
# ---------------------------------------------------------------------------

def _write_summary(
    cycle_start: datetime,
    cycle_end: datetime,
    plan: Plan,
    run_results: list[tuple[Task, AgentResult, float]],
    failures: list[str],
    outcome: Outcome,
    forced_agents: list[str],
    verdict: str = "unknown",
    retry_count: int = 0,
) -> None:
    """Write exactly one cycle_log row summarising the whole cycle (rule 33)."""
    elapsed = (cycle_end - cycle_start).total_seconds()

    durations = {task.agent_name: f"{secs:.1f}s" for task, _, secs in run_results}
    skipped   = {t.agent_name: t.reason for t in plan.skipped_tasks()}

    notes_payload: dict = {
        "outcome":     outcome,
        "elapsed_s":   round(elapsed, 2),
        "ran":         [t.agent_name for t, _, _ in run_results],
        "skipped":     skipped,
        "durations":   durations,
        "failures":    failures,
        "verdict":     verdict,      # "pass"|"fail"|"degraded"|"unknown" (rule 38)
        "retry_count": retry_count,  # 0 or 1 (rule 36 — at most one retry)
    }
    if forced_agents:
        notes_payload["forced_by_operator"] = forced_agents

    storage.log_cycle(
        agent="orchestrator",
        started_at=cycle_start.isoformat(),
        finished_at=cycle_end.isoformat(),
        records_touched=sum(r.records_touched for _, r, _ in run_results),
        status=outcome,
        notes=json.dumps(notes_payload),
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_cycle(
    config: Config,
    dry_run: bool = False,
    forced_agents: list[str] | None = None,
    explain: bool = False,
) -> None:
    """Execute one complete orchestration cycle.

    Parameters
    ----------
    config:
        Loaded project configuration.
    dry_run:
        When True, print the plan and return immediately — no execution,
        no writes, no API calls. Exit code 0.
    forced_agents:
        Agent names to force into the plan regardless of state. build_plan
        is not altered; overrides are applied after planning completes.
    explain:
        When True, print the full state → decision trace before the plan.

    Exit conditions
    ---------------
    dry_run       : printed plan, no execution — success
    nothing_to_do : plan had only skips — success
    complete      : all runnable tasks succeeded — success
    partial       : at least one task failed — logged, still exit 0
    """
    forced_agents = forced_agents or []
    cycle_start   = datetime.now(timezone.utc)

    # ── 1. Initialise storage ──────────────────────────────────────────────
    storage.init_db(config.db_path)

    # ── 2. Read state (rule 28) ────────────────────────────────────────────
    state: SystemState = read_state(config, cycle_start)

    _banner("EdgeDash — cycle starting")
    _row("Cycle started (UTC)", cycle_start.strftime("%Y-%m-%d %H:%M:%S"))
    _row("Target role",         config.target_role)
    _row("Target city",         config.target_city)
    _row("DB path",             config.db_path)
    if dry_run:
        _row("Mode",            "DRY RUN — no execution")
    if forced_agents:
        _row("Forced agents",   ", ".join(forced_agents))

    _section("State")
    _row("Last fetch",          state.last_fetch_at or "never")
    _row("Hours since fetch",
         f"{state.hours_since_fetch:.1f}" if state.hours_since_fetch is not None else "n/a")
    _row("Unscored listings",   state.unscored_count)
    _row("Gaps computed at",    state.gaps_computed_at or "never")
    _row("Gaps stale",          state.gaps_stale)
    _row("Last cycle verdict",  state.last_cycle_verdict or "none")

    # ── 3. Build plan — pure function, unaffected by flags (rule 29) ───────
    plan: Plan = build_plan(state, config)

    # ── 4. Apply --force overrides (does not alter build_plan) ────────────
    plan, applied_overrides = _apply_overrides(plan, forced_agents, config)

    if applied_overrides:
        print(
            f"\n  ⚠  PLAN MANUALLY OVERRIDDEN — "
            f"forced: {', '.join(applied_overrides)}"
        )
        logger.warning(
            "Orchestrator: plan overridden by operator — forced agents: %s",
            applied_overrides,
        )

    # ── 5. --explain: state → decision trace ──────────────────────────────
    if explain:
        _print_explain(state, plan, config)

    # ── 6. Print plan before executing anything (rule 31) ─────────────────
    _section("Plan")
    print(plan.render())

    # ── 7. --dry-run exits here — nothing written, nothing called ─────────
    if dry_run:
        _banner("Dry run complete — no agents executed")
        print("  Re-run without --dry-run to execute this plan.\n")
        logger.info("Orchestrator: dry-run complete — exiting without execution")
        return

    # ── 8. Nothing to do → exit cleanly (rule 28) ─────────────────────────
    if not plan.runnable():
        cycle_end = datetime.now(timezone.utc)
        _banner("Cycle outcome: nothing_to_do")
        print("  All agents skipped — system is up to date.\n")
        _write_summary(
            cycle_start=cycle_start,
            cycle_end=cycle_end,
            plan=plan,
            run_results=[],
            failures=[],
            outcome="nothing_to_do",
            forced_agents=applied_overrides,
        )
        logger.info("Orchestrator: nothing_to_do — all agents skipped")
        return

    # ── 9. Execute runnable tasks (rules 29, 32) ───────────────────────────
    registry = _build_registry(config)
    run_results: list[tuple[Task, AgentResult, float]] = []
    failures:    list[str] = []

    _section("Execution")

    for task in plan.tasks:
        if task.skipped:
            print(f"\n  [skip]  {task.agent_name:<12}  {task.reason}")
            logger.info(
                "Orchestrator: skipping %s — %s", task.agent_name, task.reason
            )
            continue

        print(f"\n  → {task.agent_name} starting …")
        logger.info(
            "Orchestrator: starting %s | goal: %s", task.agent_name, task.goal
        )

        try:
            result, elapsed = _run_task(task, registry, config)
        except Exception as exc:
            result = AgentResult(
                agent=task.agent_name,
                status="failed",
                records_touched=0,
                notes=f"{type(exc).__name__}: {exc}",
            )
            elapsed = 0.0
            logger.exception(
                "Orchestrator: %s raised an unhandled exception", task.agent_name
            )

        run_results.append((task, result, elapsed))

        icon = "✓" if result.status != "failed" else "✗"
        print(
            f"  {icon} {task.agent_name:<12}  "
            f"status={result.status}  "
            f"records={result.records_touched}  "
            f"({elapsed:.1f}s)"
        )
        if result.notes:
            print(f"    {result.notes}")

        if result.status == "failed":
            failures.append(f"{task.agent_name}: {result.notes}")
            logger.error(
                "Orchestrator: %s failed — %s", task.agent_name, result.notes
            )
        else:
            logger.info(
                "Orchestrator: %s ok — %s records in %.1fs",
                task.agent_name, result.records_touched, elapsed,
            )

    # ── 10. Determine outcome ──────────────────────────────────────────────
    outcome: Outcome = "partial" if failures else "complete"

    # ── 11. Verify outputs (rules 34-39) ───────────────────────────────────
    # Only verify when the pipeline itself ran without hard errors; a partial
    # cycle (agent exception) is already logged as such — no point verifying
    # incomplete data.
    verdict_str  = "unknown"
    retry_count  = 0

    if not failures and run_results:
        verdict_str, retry_count = _run_verification(
            plan, registry, config, run_results, failures
        )
        if verdict_str == "degraded":
            outcome = "degraded"
            _section("Verification")
            print(
                "  ✗ Verification failed after retry — cycle marked DEGRADED.\n"
                "  Dashboard will continue serving the last known-good data (rule 38)."
            )
            logger.warning(
                "Orchestrator: cycle degraded after verification retry"
            )
        elif verdict_str == "fail":
            # Failed but no retryable agent was identified (e.g. stale data)
            outcome = "degraded"
            _section("Verification")
            print(
                "  ✗ Verification failed (no retry applicable) — cycle DEGRADED."
            )
            logger.warning(
                "Orchestrator: cycle degraded — verification failed, no retry possible"
            )
        else:
            _section("Verification")
            print(f"  ✓ Verification passed (retries used: {retry_count})")
            logger.info(
                "Orchestrator: verification passed (retry_count=%d)", retry_count
            )

    # ── 12. Write one summary row (rule 33) ────────────────────────────────
    cycle_end     = datetime.now(timezone.utc)
    total_elapsed = (cycle_end - cycle_start).total_seconds()
    total_records = sum(r.records_touched for _, r, _ in run_results)

    _banner(f"Cycle outcome: {outcome}")
    _row("Finished (UTC)",    cycle_end.strftime("%Y-%m-%d %H:%M:%S"))
    _row("Total elapsed (s)", f"{total_elapsed:.2f}")
    _row("Records touched",   total_records)
    _row("Agents ran",        len(run_results))
    _row("Agents skipped",    len(plan.skipped_tasks()))
    if applied_overrides:
        _row("Forced overrides", ", ".join(applied_overrides))

    if failures:
        _section("Failures")
        for f in failures:
            print(f"  ✗ {f}")

    print(f"\n{'─' * _W}\n")

    _write_summary(
        cycle_start=cycle_start,
        cycle_end=cycle_end,
        plan=plan,
        run_results=run_results,
        failures=failures,
        outcome=outcome,
        forced_agents=applied_overrides,
        verdict=verdict_str,
        retry_count=retry_count,
    )
    logger.info(
        "Orchestrator: cycle %s — %.2fs, %d records, %d failures",
        outcome, total_elapsed, total_records, len(failures),
    )

# ---------------------------------------------------------------------------
# Verification helpers (rules 34-39)
# ---------------------------------------------------------------------------

def _run_verifier(
    verifier: Agent,
    config: Config,
    stop_conditions: StopConditions,
) -> AgentResult:
    """Call the Verifier and return its AgentResult.

    Exceptions are caught and turned into a failed AgentResult so an
    unexpected crash in the Verifier itself cannot hide a broken cycle.
    """
    try:
        return verifier.run(config, stop_conditions)
    except Exception as exc:  # noqa: BLE001
        msg = f"Verifier raised unexpectedly: {type(exc).__name__}: {exc}"
        logger.exception("Orchestrator: %s", msg)
        return AgentResult(
            agent="Verifier", status="failed", records_touched=0, notes=msg
        )


def _parse_failed_check(notes: str) -> str:
    """Extract the first failed check name from a Verifier notes string.

    Notes format: "VERDICT: fail — check_score_spread observed … | …"
    Returns the check name (e.g. "check_score_spread"), or "" if not found.
    """
    # Notes line after "VERDICT: fail — " is "check_name observed …"
    marker = "VERDICT: fail — "
    if marker not in notes:
        return ""
    tail = notes.split(marker, 1)[1]
    # First token before a space is the check name
    return tail.split()[0] if tail.strip() else ""


def _retry_failing_agent(
    failed_check: str,
    plan: Plan,
    registry: dict[str, Agent],
    config: Config,
    run_results: list[tuple[Task, AgentResult, float]],
    failures: list[str],
) -> bool:
    """Re-run the agent responsible for the failing check with adjusted context.

    Rule 36: at most one retry per cycle — this function is called once.
    Rule 30: the Orchestrator sets the adjusted stop_conditions; the agent
             does not decide its own retry limits.

    Returns True if a retry was attempted, False if the check has no
    retryable agent (e.g. freshness — only a new fetch fixes that, and
    we don't restart the whole cycle).

    Score-spread retry — adjusted context rationale
    ------------------------------------------------
    A spread failure means all scores landed in a tight band.  The most
    likely cause is that the current batch of listings is homogeneous
    (same seniority, same tech stack).  Re-scoring with a smaller batch
    capped at the most-recently-fetched listings gives the scoring
    function a fresher, potentially more varied slice.  We also halve
    max_items (floor 5, ceiling 15) so the retry is cheap and fast.

    Mechanically: we clear fit_score on the listings scored this cycle
    so the Scorer has rows to process, then run it with the tighter cap.
    We do NOT touch the scoring weights (rule 16 — weights are config,
    not runtime state).  If the distribution is still tight after this,
    that is a genuine signal about the data, not a bug, and the cycle
    degrades correctly.
    """
    score_checks = {"check_score_spread"}
    gap_checks   = {"check_gap_sample_size", "check_extraction_sanity"}

    if failed_check in score_checks:
        logger.warning(
            "Orchestrator: retrying scorer — check_score_spread failed, "
            "using smaller batch to target fresher, more varied listings"
        )
        print(
            f"\n  ↺ Retrying scorer (check_score_spread failed — "
            f"smaller batch, fresher listings)"
        )
        _clear_current_batch_scores(run_results)
        retry_batch = max(5, min(15, config.scoring_batch_size // 2))
        adj_stop = StopConditions(
            max_items=retry_batch,
            max_seconds=config.score_max_seconds,
        )
        retry_task = Task(
            agent_name="scorer",
            goal="Retry: score a smaller, fresher batch to widen distribution",
            stop_conditions=adj_stop,
            reason="retry after check_score_spread failure",
        )
        try:
            result, elapsed = _run_task(retry_task, registry, config)
        except Exception as exc:  # noqa: BLE001
            msg = f"scorer retry failed: {type(exc).__name__}: {exc}"
            logger.error("Orchestrator: %s", msg)
            failures.append(msg)
            return True  # retry was attempted, even if it failed

        run_results.append((
            Task(
                agent_name="scorer",
                goal="Retry: score a smaller fresher batch",
                stop_conditions=adj_stop,
                reason="retry after check_score_spread failure",
            ),
            result,
            elapsed,
        ))
        icon = "✓" if result.status != "failed" else "✗"
        print(f"  {icon} scorer (retry)  status={result.status}  ({elapsed:.1f}s)")
        return True

    if failed_check in gap_checks:
        # Gap/extraction failures: re-run the gap analyser so it picks up
        # any fixes the Scorer may have written in a preceding step.
        logger.warning(
            "Orchestrator: retrying analyser — %s failed", failed_check
        )
        print(f"\n  ↺ Retrying analyser ({failed_check} failed)")
        analyser_task = Task(
            agent_name="analyser",
            goal="Retry: recompute gap snapshot after extraction check failure",
            stop_conditions=StopConditions(
                max_seconds=config.analyse_max_seconds,
            ),
            reason=f"retry after {failed_check} failure",
        )
        try:
            result, elapsed = _run_task(analyser_task, registry, config)
        except Exception as exc:  # noqa: BLE001
            msg = f"analyser retry failed: {type(exc).__name__}: {exc}"
            logger.error("Orchestrator: %s", msg)
            failures.append(msg)
            return True

        run_results.append((analyser_task, result, elapsed))
        icon = "✓" if result.status != "failed" else "✗"
        print(f"  {icon} analyser (retry)  status={result.status}  ({elapsed:.1f}s)")
        return True

    # check_freshness and unknown checks: no agent retry can fix these.
    logger.warning(
        "Orchestrator: check '%s' has no retryable agent — skipping retry",
        failed_check,
    )
    return False


def _run_verification(
    plan: Plan,
    registry: dict[str, Agent],
    config: Config,
    run_results: list[tuple[Task, AgentResult, float]],
    failures: list[str],
) -> tuple[str, int]:
    """Run the Verifier; on fail, retry once; verify again.

    Returns (verdict_str, retry_count).
    verdict_str is one of: "pass", "fail", "degraded".
    retry_count is 0 or 1 (rule 36 — never more than one retry).
    """
    verifier = registry["verifier"]
    sc = StopConditions()

    # First verification pass
    v_result = _run_verifier(verifier, config, sc)
    if v_result.status == "ok":
        return "pass", 0

    logger.warning(
        "Orchestrator: first verification failed — %s", v_result.notes
    )
    print(f"\n  ✗ Verification failed: {v_result.notes}")

    # Identify which check failed and attempt one retry
    failed_check = _parse_failed_check(v_result.notes)
    retried = _retry_failing_agent(
        failed_check, plan, registry, config, run_results, failures
    )

    if not retried:
        # No agent can fix this (e.g. freshness) — degrade immediately
        return "fail", 0

    # Second and final verification pass (rule 36 — this is the last chance)
    v_result2 = _run_verifier(verifier, config, sc)
    if v_result2.status == "ok":
        logger.info("Orchestrator: verification passed after 1 retry")
        return "pass", 1

    # Still failing — cycle is degraded, stop (rule 36)
    logger.error(
        "Orchestrator: verification still failed after retry — degrading cycle. %s",
        v_result2.notes,
    )
    return "degraded", 1


def _clear_current_batch_scores(
    run_results: list[tuple[Task, AgentResult, float]],
) -> None:
    """Reset fit_score to NULL for listings scored in this cycle's scorer run.

    This gives the retry scorer fresh rows to work on.  We identify the
    batch by reading the most recently scored listings (up to batch_size)
    and clearing only those — we never touch listings scored in prior cycles.
    """
    # Find the scorer's records_touched from this cycle's run_results
    batch_size = 0
    for task, result, _ in run_results:
        if task.agent_name == "scorer":
            batch_size = result.records_touched
            break

    if batch_size == 0:
        return

    # Clear scores on the most recently scored listings only
    with storage._cursor() as cur:  # noqa: SLF001 — intentional internal access
        cur.execute(
            """
            UPDATE listings
               SET fit_score        = NULL,
                   fit_reason       = NULL,
                   score_components = NULL,
                   scored_at        = NULL
             WHERE id IN (
                SELECT id FROM listings
                 WHERE scored_at IS NOT NULL
                 ORDER BY scored_at DESC
                 LIMIT ?
             )
            """,
            (batch_size,),
        )
    logger.info(
        "Orchestrator: cleared scores for %d listings before scorer retry",
        batch_size,
    )
