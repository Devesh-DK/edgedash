"""
edgedash/skills.py — deterministic skill name canonicalisation (steering rule 23).

No LLM. No network. No imports from llm.py. Pure functions only.

Public API
----------
    canonical(raw, aliases) -> str
        Normalise one raw skill string to its canonical form.

    canonical_list(skills, aliases) -> list[str]
        Convenience wrapper: canonicalise a list and deduplicate, preserving order.

CLI
---
    python -m edgedash.skills --audit
        Read every extracted required_skills value from the database and print:
          - the 40 most common raw strings with counts and their canonical form
          - a separated list of strings seen only once (typos / junk / sentences)
        Read-only. Use this to discover aliases that are missing from the map.
"""

from __future__ import annotations

import re
from typing import List


# ---------------------------------------------------------------------------
# Normalisation pipeline
# ---------------------------------------------------------------------------

# Punctuation that may surround a skill token — stripped from both ends.
# Does NOT include '/' because "ci/cd" is a meaningful token.
_OUTER_PUNCT = re.compile(r'^[^\w/]+|[^\w/]+$')

# A parenthetical qualifier: anything inside ( … ) including the parens.
# e.g. "kubernetes (eks)" → "kubernetes"
_PARENS = re.compile(r'\s*\([^)]*\)')

# One or more internal whitespace characters → single space.
_WHITESPACE = re.compile(r'\s+')


def canonical(raw: str, aliases: dict[str, str]) -> str:
    """Return the canonical form of a raw skill string.

    Pipeline (in order):
      1. Lowercase
      2. Strip leading/trailing whitespace
      3. Drop parenthetical qualifiers — "kubernetes (eks)" → "kubernetes"
      4. Strip surrounding punctuation (but preserve '/' inside tokens)
      5. Collapse internal whitespace to a single space
      6. Apply the alias map (exact match on the normalised form)

    Args:
        raw:     The raw skill string from an extractor or user input.
        aliases: A dict mapping normalised strings to their canonical forms.
                 Loaded from config.yaml → skill_aliases.
                 Keys should already be in normalised form (lowercase, trimmed).

    Returns:
        The canonical skill name as a non-empty lowercase string.
        Returns "" only when the input is empty or pure punctuation/whitespace.

    Examples:
        >>> canonical("Kubernetes (EKS)", {"k8s": "kubernetes"})
        'kubernetes'
        >>> canonical("  PostGres  ", {"postgres": "postgres", "postgresql": "postgres"})
        'postgres'
        >>> canonical("CI/CD", {"ci/cd": "ci/cd", "cicd": "ci/cd"})
        'ci/cd'
    """
    if not raw:
        return ""

    # 1 — lowercase
    s = raw.lower()

    # 2 — strip outer whitespace
    s = s.strip()

    # 3 — drop parenthetical qualifiers BEFORE stripping outer punctuation,
    #     so the closing ')' is still present for the regex to match.
    s = _PARENS.sub("", s)

    # 4 — strip surrounding punctuation (preserves '/' mid-token)
    s = _OUTER_PUNCT.sub("", s)

    # 5 — collapse internal whitespace
    s = _WHITESPACE.sub(" ", s).strip()

    if not s:
        return ""

    # 6 — alias lookup (exact match on already-normalised form)
    return aliases.get(s, s)


def canonical_list(skills: List[str], aliases: dict[str, str]) -> List[str]:
    """Canonicalise a list of raw skill strings and deduplicate, preserving order.

    Empty strings produced by canonical() are dropped silently.
    """
    seen: set[str] = set()
    result: list[str] = []
    for raw in skills:
        c = canonical(raw, aliases)
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# CLI — python -m edgedash.skills --audit
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import collections
    import json
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    parser = argparse.ArgumentParser(
        description="Audit raw skill strings stored in the extraction cache."
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        required=True,
        help="Print the most-common and singleton raw skill strings.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=40,
        help="How many top skills to show (default 40).",
    )
    args = parser.parse_args()

    from edgedash.config import load_config
    import edgedash.storage as storage

    cfg = load_config()
    storage.init_db(cfg.db_path)
    aliases: dict[str, str] = getattr(cfg, "skill_aliases", {})

    # ── collect every required_skills entry from extraction_cache ─────────
    raw_counter: collections.Counter = collections.Counter()

    with storage._cursor() as cur:  # noqa: SLF001 — read-only, no write path
        cur.execute("SELECT extracted_json FROM extraction_cache")
        for (extracted_json,) in cur.fetchall():
            try:
                data = json.loads(extracted_json)
            except (json.JSONDecodeError, TypeError):
                continue
            for skill in data.get("required_skills") or []:
                if skill and skill.strip():
                    raw_counter[skill.strip()] += 1

    if not raw_counter:
        print("No extracted skills found in the database yet.")
        raise SystemExit(0)

    total_unique = len(raw_counter)
    total_occurrences = sum(raw_counter.values())

    # ── top N ─────────────────────────────────────────────────────────────
    top_n = args.top
    print(f"\n{'─' * 68}")
    print(f"  TOP {top_n} RAW SKILL STRINGS  "
          f"({total_occurrences} total occurrences, {total_unique} unique)")
    print(f"{'─' * 68}")
    print(f"  {'COUNT':>6}  {'RAW STRING':<35}  CANONICAL")
    print(f"  {'─'*6}  {'─'*35}  {'─'*25}")

    for raw_str, count in raw_counter.most_common(top_n):
        canon = canonical(raw_str, aliases)
        marker = "  ← aliased" if canon != raw_str.lower().strip() else ""
        print(f"  {count:>6}  {raw_str:<35}  {canon}{marker}")

    # ── singletons ────────────────────────────────────────────────────────
    singletons = sorted(s for s, c in raw_counter.items() if c == 1)
    print(f"\n{'─' * 68}")
    print(f"  SINGLETONS ({len(singletons)}) — seen exactly once")
    print(f"  Typos, junk, or full sentences the extractor mis-classified.")
    print(f"  Add real ones to skill_aliases in config.yaml; ignore the rest.")
    print(f"{'─' * 68}")
    for s in singletons:
        canon = canonical(s, aliases)
        marker = f"  → {canon}" if canon != s.lower().strip() else ""
        print(f"  {s}{marker}")

    print(f"\n{'─' * 68}\n")
