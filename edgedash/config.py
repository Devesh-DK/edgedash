"""
Load and expose project configuration from config.yaml at the repo root.

Usage:
    from edgedash.config import load_config
    cfg = load_config()          # reads <repo_root>/config.yaml
    cfg = load_config("/custom/path/config.yaml")
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

# PyYAML is the one dependency this module adds; it genuinely saves the work
# of writing a YAML parser and is the de-facto standard for YAML in Python.
try:
    import yaml
except ImportError as exc:  # noqa: BLE001
    sys.exit(
        "PyYAML is required but not installed. "
        "Run: pip install pyyaml\n"
        f"Original error: {exc}"
    )

_REPO_ROOT = Path(__file__).resolve().parent.parent
_env_path = _REPO_ROOT / ".env"
if _env_path.is_file():
    load_dotenv(_env_path)
else:
    load_dotenv()

# ---------------------------------------------------------------------------
# Defaults — applied when a field is absent from config.yaml
# ---------------------------------------------------------------------------
_DEFAULTS: dict = {
    "target_role": "Software Engineer",
    "target_city": "Remote",
    "target_seniority": "mid",
    "keywords": [],
    "my_skills": [],
    "experience_years": 0,
    "db_path": "edgedash.db",
    "min_fit_score": 60,
    "sources": ["arbeitnow"],
    "use_mock_fetcher": False,
    # LLM settings (steering rule 15)
    "llm_provider": "gemini",
    "llm_model": "gemini-3.5-flash-lite",
    "llm_requests_per_second": 4,
    "llm_requests_per_minute": 14,
    "scoring_batch_size": 25,
    # Orchestration thresholds (steering rules 28-33)
    "fetch_interval_hours": 6,
    "fetch_max_pages": 5,
    "fetch_max_listings": 200,
    "score_max_seconds": 300,
    "analyse_max_seconds": 120,
    # Scoring weights (steering rules 16, 19) — must sum to 1.0
    "weight_skill_match": 0.45,
    "weight_seniority_fit": 0.25,
    "weight_location_fit": 0.15,
    "weight_recency": 0.15,
    # Skill alias map (steering rule 23) — seeded in config.yaml
    "skill_aliases": {},
    # Verification thresholds (steering rules 34-39)
    "min_score_spread": 10,
    "min_score_stdev": 5,
    "max_empty_extraction_pct": 20,
    "max_skills_per_listing": 100,
    "min_gap_sample": 3,
    "max_data_age_days": 3,
    # Ask endpoint abuse guards
    "daily_ask_cap": 200,
}


@dataclass
class Config:
    target_role: str
    target_city: str
    target_seniority: str
    keywords: List[str]
    my_skills: List[str]
    experience_years: int
    db_path: str
    min_fit_score: int
    sources: List[str]
    use_mock_fetcher: bool
    # LLM settings
    llm_provider: str
    llm_model: str
    llm_requests_per_second: int
    llm_requests_per_minute: int
    scoring_batch_size: int
    # Orchestration thresholds
    fetch_interval_hours: int
    fetch_max_pages: int
    fetch_max_listings: int
    score_max_seconds: int
    analyse_max_seconds: int
    # Scoring weights
    weight_skill_match: float
    weight_seniority_fit: float
    weight_location_fit: float
    weight_recency: float
    # Skill alias map (steering rule 23)
    skill_aliases: Dict[str, str]
    # Verification thresholds (steering rules 34-39)
    min_score_spread: float
    min_score_stdev: float
    max_empty_extraction_pct: float
    max_skills_per_listing: int
    min_gap_sample: int
    max_data_age_days: int
    # Ask endpoint abuse guards
    daily_ask_cap: int = 200

    def override_from_profile(self, profile: dict) -> None:
        """Dynamically overwrite config with user profile from DB."""
        if profile.get("target_job"):
            self.target_role = profile["target_job"]
        if profile.get("skills"):
            self.my_skills = profile["skills"]
        # The keywords can be overwritten or appended to based on suited_profiles
        if profile.get("suited_profiles"):
            self.keywords = profile["suited_profiles"]



def load_config(path: str | Path | None = None) -> Config:
    """Read config.yaml and return a validated Config instance.

    Raises FileNotFoundError with a clear message when config.yaml is absent.
    Missing fields fall back to sensible defaults; extra keys are ignored.
    """
    config_path = Path(path) if path else _REPO_ROOT / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at '{config_path}'.\n"
            "Copy config.yaml from the repo root and fill in your profile."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    merged = {**_DEFAULTS, **raw}

    return Config(
        target_role=str(merged["target_role"]),
        target_city=str(merged["target_city"]),
        target_seniority=str(merged["target_seniority"]),
        keywords=list(merged["keywords"]),
        my_skills=list(merged["my_skills"]),
        experience_years=int(merged["experience_years"]),
        db_path=str(merged["db_path"]),
        min_fit_score=int(merged["min_fit_score"]),
        sources=list(merged["sources"]),
        use_mock_fetcher=bool(merged["use_mock_fetcher"]),
        llm_provider=str(merged["llm_provider"]),
        llm_model=str(merged["llm_model"]),
        llm_requests_per_second=int(merged["llm_requests_per_second"]),
        llm_requests_per_minute=int(merged["llm_requests_per_minute"]),
        scoring_batch_size=int(merged["scoring_batch_size"]),
        fetch_interval_hours=int(merged["fetch_interval_hours"]),
        fetch_max_pages=int(merged["fetch_max_pages"]),
        fetch_max_listings=int(merged["fetch_max_listings"]),
        score_max_seconds=int(merged["score_max_seconds"]),
        analyse_max_seconds=int(merged["analyse_max_seconds"]),
        weight_skill_match=float(merged["weight_skill_match"]),
        weight_seniority_fit=float(merged["weight_seniority_fit"]),
        weight_location_fit=float(merged["weight_location_fit"]),
        weight_recency=float(merged["weight_recency"]),
        skill_aliases=dict(merged.get("skill_aliases") or {}),
        min_score_spread=float(merged["min_score_spread"]),
        min_score_stdev=float(merged["min_score_stdev"]),
        max_empty_extraction_pct=float(merged["max_empty_extraction_pct"]),
        max_skills_per_listing=int(merged["max_skills_per_listing"]),
        min_gap_sample=int(merged["min_gap_sample"]),
        max_data_age_days=int(merged["max_data_age_days"]),
        daily_ask_cap=int(merged["daily_ask_cap"]),
    )
