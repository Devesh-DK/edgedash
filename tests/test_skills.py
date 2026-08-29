"""
tests/test_skills.py — unit tests for edgedash/skills.py.

canonical() is a pure function: no I/O, no DB, no network.
Every test calls it directly with an explicit alias dict.

Covered cases (as specified):
  1. case             — mixed-case input is lowercased
  2. whitespace       — leading/trailing/internal whitespace is normalised
  3. parentheses      — parenthetical qualifiers are stripped
  4. aliased term     — a key present in the alias map returns its canonical form
  5. no alias         — a term absent from the alias map returns itself (normalised)
  6. empty string     — empty / whitespace-only input returns ""

Additional cases that fall naturally out of the pipeline:
  7. surrounding punctuation stripped
  8. canonical_list() deduplication and ordering
  9. ci/cd — slash preserved mid-token, cicd alias applied
"""

from __future__ import annotations

import pytest

from edgedash.skills import canonical, canonical_list

# Shared alias dict used across all tests — mirrors the real config structure.
_ALIASES: dict[str, str] = {
    "k8s": "kubernetes",
    "postgresql": "postgres",
    "psql": "postgres",
    "node": "node.js",
    "nodejs": "node.js",
    "node js": "node.js",
    "gcp": "gcp",
    "google cloud": "gcp",
    "ml": "machine learning",
    "cicd": "ci/cd",
    "ci cd": "ci/cd",
    "ci-cd": "ci/cd",
}


# ---------------------------------------------------------------------------
# 1. Case normalisation
# ---------------------------------------------------------------------------

def test_case_lowercased() -> None:
    """Mixed-case input must be lowercased before alias lookup and output."""
    assert canonical("Python", _ALIASES) == "python"
    assert canonical("KUBERNETES", _ALIASES) == "kubernetes"
    assert canonical("TaBLeaU", _ALIASES) == "tableau"


# ---------------------------------------------------------------------------
# 2. Whitespace normalisation
# ---------------------------------------------------------------------------

def test_whitespace_stripped() -> None:
    """Leading and trailing whitespace is stripped."""
    assert canonical("  python  ", _ALIASES) == "python"
    assert canonical("\tSQL\n", _ALIASES) == "sql"


def test_internal_whitespace_collapsed() -> None:
    """Multiple internal spaces are collapsed to one."""
    assert canonical("machine   learning", _ALIASES) == "machine learning"
    assert canonical("node  js", _ALIASES) == "node.js"  # collapses then alias hits


# ---------------------------------------------------------------------------
# 3. Parenthetical qualifiers stripped
# ---------------------------------------------------------------------------

def test_parens_stripped() -> None:
    """Content inside parentheses (including the parens) is removed."""
    assert canonical("Kubernetes (EKS)", _ALIASES) == "kubernetes"
    assert canonical("kubernetes (gke)", _ALIASES) == "kubernetes"


def test_parens_with_no_alias() -> None:
    """Parens stripping works even when there is no alias entry."""
    assert canonical("Docker (Swarm)", {}) == "docker"


# ---------------------------------------------------------------------------
# 4. Aliased term
# ---------------------------------------------------------------------------

def test_alias_applied() -> None:
    """A key present in the alias map is replaced by its canonical form."""
    assert canonical("k8s", _ALIASES) == "kubernetes"
    assert canonical("postgresql", _ALIASES) == "postgres"
    assert canonical("psql", _ALIASES) == "postgres"
    assert canonical("ml", _ALIASES) == "machine learning"
    assert canonical("google cloud", _ALIASES) == "gcp"


def test_alias_applied_after_normalisation() -> None:
    """Alias lookup happens AFTER full normalisation so case/space variants hit."""
    assert canonical("K8S", _ALIASES) == "kubernetes"        # case
    assert canonical("  k8s  ", _ALIASES) == "kubernetes"    # whitespace
    assert canonical("PostgreSQL", _ALIASES) == "postgres"   # case on alias key


# ---------------------------------------------------------------------------
# 5. Term with no alias — returns normalised self
# ---------------------------------------------------------------------------

def test_no_alias_returns_normalised() -> None:
    """A term absent from the alias map is returned in normalised form."""
    assert canonical("Pandas", _ALIASES) == "pandas"
    assert canonical("NumPy", _ALIASES) == "numpy"
    assert canonical("Tableau", _ALIASES) == "tableau"


# ---------------------------------------------------------------------------
# 6. Empty string
# ---------------------------------------------------------------------------

def test_empty_string_returns_empty() -> None:
    """Empty input must return \"\" without raising."""
    assert canonical("", _ALIASES) == ""
    assert canonical("", {}) == ""


def test_whitespace_only_returns_empty() -> None:
    """Whitespace-only input produces \"\" after stripping."""
    assert canonical("   ", _ALIASES) == ""
    assert canonical("\t\n", _ALIASES) == ""


def test_pure_punctuation_returns_empty() -> None:
    """Input that is only punctuation should return \"\" after stripping."""
    assert canonical("...", _ALIASES) == ""
    assert canonical("---", _ALIASES) == ""


# ---------------------------------------------------------------------------
# 7. Surrounding punctuation stripped (slash preserved mid-token)
# ---------------------------------------------------------------------------

def test_surrounding_punctuation_stripped() -> None:
    """Punctuation at the edges is removed; internal slash is kept."""
    assert canonical(".python.", _ALIASES) == "python"
    assert canonical("-sql-", _ALIASES) == "sql"


def test_slash_preserved_midtoken() -> None:
    """Slash inside a token (ci/cd) must survive the outer-punct strip."""
    assert canonical("CI/CD", _ALIASES) == "ci/cd"      # normalised, no alias needed
    assert canonical("CICD", _ALIASES) == "ci/cd"       # alias applied
    assert canonical("ci-cd", _ALIASES) == "ci/cd"      # hyphen alias
    assert canonical("ci cd", _ALIASES) == "ci/cd"      # space alias


# ---------------------------------------------------------------------------
# 8. canonical_list() — deduplication and ordering
# ---------------------------------------------------------------------------

def test_canonical_list_deduplicates() -> None:
    """The same canonical form appearing multiple times is kept only once."""
    raw = ["k8s", "Kubernetes", "KUBERNETES"]
    result = canonical_list(raw, _ALIASES)
    assert result == ["kubernetes"]


def test_canonical_list_preserves_order() -> None:
    """First occurrence order is preserved after deduplication."""
    raw = ["pandas", "sql", "postgresql", "Pandas"]
    result = canonical_list(raw, _ALIASES)
    assert result == ["pandas", "sql", "postgres"]


def test_canonical_list_drops_empty() -> None:
    """Empty strings produced by canonical() are not included in the output."""
    raw = ["python", "", "   ", "sql"]
    result = canonical_list(raw, _ALIASES)
    assert result == ["python", "sql"]


def test_canonical_list_empty_input() -> None:
    """An empty input list returns an empty list without raising."""
    assert canonical_list([], _ALIASES) == []
