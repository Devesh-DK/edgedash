"""
Deterministic verification checks for EdgeDash cycle output.

Rules 34-39: The Verifier judges plausibility only — it never repairs data.
Every function is pure: no clock, no network, no database reads.
Thresholds come from config (rule 39).
No LLM anywhere in this file.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Sequence

from edgedash.config import Config


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CheckResult:
    name: str           # identifies which check fired in cycle_log (rule 37)
    passed: bool
    observed: str       # the value that was tested — never just "failed" (rule 37)
    threshold: str      # the limit it was tested against
    message: str        # human-readable verdict sentence


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failed_checks: List[CheckResult]
    summary: str        # one-line summary for cycle_log


# ---------------------------------------------------------------------------
# Check 1 — score spread
# ---------------------------------------------------------------------------

def check_score_spread(scores: Sequence[float], config: Config) -> CheckResult:
    """Fail if scores are too tightly clustered to give the user any signal.

    Passes trivially (with a note) when fewer than 5 scores are present
    because spread statistics are meaningless on tiny samples.
    """
    name = "check_score_spread"
    n = len(scores)

    if n < 5:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"n={n}",
            threshold="n>=5 required for spread check",
            message=f"Trivial pass: only {n} score(s) — spread check needs at least 5.",
        )

    spread = max(scores) - min(scores)
    stdev = statistics.stdev(scores)

    if spread < config.min_score_spread:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"spread={spread:.1f}",
            threshold=f"min_score_spread={config.min_score_spread}",
            message=(
                f"Score spread {spread:.1f} is below threshold "
                f"{config.min_score_spread} — possible score inflation."
            ),
        )

    if stdev < config.min_score_stdev:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"stdev={stdev:.2f}",
            threshold=f"min_score_stdev={config.min_score_stdev}",
            message=(
                f"Score stdev {stdev:.2f} is below threshold "
                f"{config.min_score_stdev} — distribution is suspiciously tight."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"spread={spread:.1f}, stdev={stdev:.2f}",
        threshold=(
            f"min_score_spread={config.min_score_spread}, "
            f"min_score_stdev={config.min_score_stdev}"
        ),
        message=f"Score spread {spread:.1f} and stdev {stdev:.2f} are within bounds.",
    )


# ---------------------------------------------------------------------------
# Check 2 — extraction sanity
# ---------------------------------------------------------------------------

def check_extraction_sanity(
    facts_list: Sequence[dict], config: Config
) -> CheckResult:
    """Fail if the extractor is broken or the model returned prose as skills.

    Each item in facts_list must have a 'required_skills' key containing a
    list (may be empty).
    """
    name = "check_extraction_sanity"
    n = len(facts_list)

    if n == 0:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold="n>0 required for extraction check",
            message="Trivial pass: no facts to check.",
        )

    empty_count = sum(
        1 for f in facts_list if not f.get("required_skills")
    )
    empty_pct = (empty_count / n) * 100

    if empty_pct > config.max_empty_extraction_pct:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"empty_pct={empty_pct:.1f}% ({empty_count}/{n})",
            threshold=f"max_empty_extraction_pct={config.max_empty_extraction_pct}%",
            message=(
                f"{empty_pct:.1f}% of listings have empty required_skills "
                f"(threshold {config.max_empty_extraction_pct}%) — extractor may be broken."
            ),
        )

    oversized = [
        i for i, f in enumerate(facts_list)
        if len(f.get("required_skills") or []) > config.max_skills_per_listing
    ]
    if oversized:
        worst = max(
            len(f.get("required_skills") or []) for f in facts_list
        )
        return CheckResult(
            name=name,
            passed=False,
            observed=f"max_skills_seen={worst} (listings {oversized})",
            threshold=f"max_skills_per_listing={config.max_skills_per_listing}",
            message=(
                f"{len(oversized)} listing(s) have >{config.max_skills_per_listing} skills "
                f"(worst={worst}) — model likely returned prose instead of a list."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"empty_pct={empty_pct:.1f}%, max_skills={max((len(f.get('required_skills') or []) for f in facts_list), default=0)}",
        threshold=(
            f"max_empty_extraction_pct={config.max_empty_extraction_pct}%, "
            f"max_skills_per_listing={config.max_skills_per_listing}"
        ),
        message=f"Extraction looks sane: {empty_pct:.1f}% empty, all skill lists within size limit.",
    )


# ---------------------------------------------------------------------------
# Check 3 — gap sample size
# ---------------------------------------------------------------------------

def check_gap_sample_size(gaps: Sequence[dict], config: Config) -> CheckResult:
    """Fail if the top-ranked gap comes from too few listings to be credible.

    Each gap dict must have a 'sample_size' key (int) and a 'rank' key (int,
    lower = better) or be ordered by rank already (index 0 = top).
    """
    name = "check_gap_sample_size"

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold=f"min_gap_sample={config.min_gap_sample}",
            message="Trivial pass: no gaps to check.",
        )

    # Accept either pre-sorted list or dicts with a 'rank' field.
    top = min(gaps, key=lambda g: g.get("rank", 0)) if "rank" in gaps[0] else gaps[0]
    sample = top.get("sample_size", 0)

    if sample < config.min_gap_sample:
        skill = top.get("skill", "<unknown>")
        return CheckResult(
            name=name,
            passed=False,
            observed=f"top_gap='{skill}', sample_size={sample}",
            threshold=f"min_gap_sample={config.min_gap_sample}",
            message=(
                f"Top-ranked gap '{skill}' is backed by only {sample} listing(s) "
                f"(threshold {config.min_gap_sample}) — ranking a rumour."
            ),
        )

    skill = top.get("skill", "<unknown>")
    return CheckResult(
        name=name,
        passed=True,
        observed=f"top_gap='{skill}', sample_size={sample}",
        threshold=f"min_gap_sample={config.min_gap_sample}",
        message=f"Top gap '{skill}' backed by {sample} listing(s) — sufficient sample.",
    )


# ---------------------------------------------------------------------------
# Check 4 — data freshness
# ---------------------------------------------------------------------------

def check_freshness(
    latest_fetch_at: datetime | None,
    config: Config,
    now: datetime,
) -> CheckResult:
    """Fail if the newest listing is older than max_data_age_days.

    `now` is a parameter — never call datetime.now() inside this function
    so the check remains deterministic and testable.
    Both datetimes must be timezone-aware or both naive; mixing raises ValueError.
    """
    name = "check_freshness"

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed="latest_fetch_at=None",
            threshold=f"max_data_age_days={config.max_data_age_days}",
            message="No fetch timestamp found — data freshness cannot be confirmed.",
        )

    age_days = (now - latest_fetch_at).total_seconds() / 86_400

    if age_days > config.max_data_age_days:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"age={age_days:.2f}d (latest_fetch_at={latest_fetch_at.isoformat()})",
            threshold=f"max_data_age_days={config.max_data_age_days}",
            message=(
                f"Newest listing is {age_days:.1f} day(s) old "
                f"(threshold {config.max_data_age_days}d) — fetch may have silently failed."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"age={age_days:.2f}d",
        threshold=f"max_data_age_days={config.max_data_age_days}",
        message=f"Data is fresh: newest listing is {age_days:.2f} day(s) old.",
    )


# ---------------------------------------------------------------------------
# run_all_checks — collects every result into a single Verdict
# ---------------------------------------------------------------------------

def run_all_checks(
    scores: Sequence[float],
    facts_list: Sequence[dict],
    gaps: Sequence[dict],
    latest_fetch_at: datetime | None,
    config: Config,
    now: datetime,
) -> Verdict:
    """Run all verification checks and return a single Verdict.

    Passed only if every check passes (rule 38).
    Collects all results so the caller sees every failure at once.
    """
    results = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    passed = len(failed) == 0

    if passed:
        summary = f"All {len(results)} checks passed."
    else:
        names = ", ".join(r.name for r in failed)
        summary = f"{len(failed)}/{len(results)} check(s) failed: {names}."

    return Verdict(passed=passed, failed_checks=failed, summary=summary)
