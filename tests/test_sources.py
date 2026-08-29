"""
tests/test_sources.py — Unit tests for job sources (RemoteOK, Jobicy, Arbeitnow).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from edgedash.config import Config
from edgedash.sources.base import NORMALISED_KEYS, SOURCES
from edgedash.sources.http import SourceError
from edgedash.sources.hasjob import HasjobSource
from edgedash.sources.jobicy import JobicySource
from edgedash.sources.remoteok import RemoteOKSource


@pytest.fixture
def mock_config():
    return Config(
        target_role="Data Analyst",
        target_city="Remote",
        target_seniority="mid",
        keywords=["Python", "SQL"],
        my_skills=["Python", "SQL"],
        experience_years=3,
        db_path=":memory:",
        min_fit_score=60,
        sources=["arbeitnow", "remoteok", "jobicy", "hasjob"],
        use_mock_fetcher=False,
        llm_provider="gemini",
        llm_model="gemini-3.5-flash-lite",
        llm_requests_per_second=1,
        llm_requests_per_minute=15,
        scoring_batch_size=25,
        fetch_interval_hours=6,
        fetch_max_pages=5,
        fetch_max_listings=200,
        score_max_seconds=300,
        analyse_max_seconds=120,
        weight_skill_match=0.45,
        weight_seniority_fit=0.25,
        weight_location_fit=0.15,
        weight_recency=0.15,
        skill_aliases={},
        min_score_spread=10.0,
        min_score_stdev=5.0,
        max_empty_extraction_pct=20.0,
        max_skills_per_listing=100,
        min_gap_sample=3,
        max_data_age_days=3,
        daily_ask_cap=200,
    )


def test_sources_registered():
    assert "arbeitnow" in SOURCES
    assert "remoteok" in SOURCES
    assert "jobicy" in SOURCES
    assert "hasjob" in SOURCES


def test_remoteok_source_success(mock_config):
    mock_payload = [
        {"legal": "Notice text"},
        {
            "id": "101",
            "slug": "101-python-data-analyst",
            "epoch": 1700000000,
            "date": "2024-05-01T12:00:00Z",
            "company": "DataCorp",
            "position": "Python Data Analyst",
            "tags": ["python", "sql", "analyst"],
            "description": "Work with SQL and Python data pipelines.",
            "location": "Worldwide",
            "url": "https://remoteok.com/job/101",
        },
        {
            "id": "102",
            "slug": "102-sales-rep",
            "epoch": 1700000000,
            "company": "SalesCorp",
            "position": "Account Executive",
            "tags": ["sales", "crm"],
            "description": "Cold calling and outbound sales.",
            "location": "Worldwide",
            "url": "https://remoteok.com/job/102",
        },
    ]

    with patch("edgedash.sources.remoteok.get_json", return_value=mock_payload):
        source = RemoteOKSource()
        rows = source.fetch(mock_config)

        assert len(rows) == 1
        row = rows[0]
        for key in NORMALISED_KEYS:
            assert key in row

        assert row["source"] == "remoteok"
        assert row["external_id"] == "101"
        assert row["title"] == "Python Data Analyst"
        assert row["company"] == "DataCorp"
        assert "SQL" in row["description"]


def test_remoteok_source_http_error(mock_config):
    with patch("edgedash.sources.remoteok.get_json", side_effect=SourceError("Connection timed out")):
        source = RemoteOKSource()
        rows = source.fetch(mock_config)
        assert rows == []


def test_jobicy_source_success(mock_config):
    mock_payload = {
        "jobs": [
            {
                "id": 201,
                "url": "https://jobicy.com/jobs/201-senior-python-engineer",
                "jobTitle": "Senior Python Engineer",
                "companyName": "TechFlow",
                "jobGeo": "Anywhere (100% Remote)",
                "jobDescription": "Build data pipelines using Python and SQL databases.",
                "pubDate": "2024-05-02T10:00:00Z",
                "jobCategories": ["Engineering", "Data"],
            },
            {
                "id": 202,
                "url": "https://jobicy.com/jobs/202-content-writer",
                "jobTitle": "Content Writer",
                "companyName": "MediaWorks",
                "jobGeo": "Remote",
                "jobDescription": "Write SEO blog posts.",
                "pubDate": "2024-05-02T11:00:00Z",
                "jobCategories": ["Marketing"],
            },
        ]
    }

    with patch("edgedash.sources.jobicy.get_json", return_value=mock_payload):
        source = JobicySource()
        rows = source.fetch(mock_config)

        assert len(rows) == 1
        row = rows[0]
        for key in NORMALISED_KEYS:
            assert key in row

        assert row["source"] == "jobicy"
        assert row["external_id"] == "201"
        assert row["title"] == "Senior Python Engineer"
        assert row["company"] == "TechFlow"


def test_jobicy_source_http_error(mock_config):
    with patch("edgedash.sources.jobicy.get_json", side_effect=SourceError("500 Server Error")):
        source = JobicySource()
        rows = source.fetch(mock_config)
        assert rows == []


def test_hasjob_source_success(mock_config):
    mock_xml = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <title>All jobs – Hasjob</title>
      <entry>
        <title>Lead Python &amp; SQL Data Engineer</title>
        <id>https://hasjob.co/dataflow.ai/abc12</id>
        <link href="https://hasjob.co/dataflow.ai/abc12"/>
        <published>2026-08-27T09:00:00Z</published>
        <location>Bengaluru, India</location>
        <content type="html">&lt;p&gt;Looking for Python, SQL and ETL experts in Bengaluru.&lt;/p&gt;</content>
      </entry>
      <entry>
        <title>HR Manager</title>
        <id>https://hasjob.co/hrcorp.com/def34</id>
        <link href="https://hasjob.co/hrcorp.com/def34"/>
        <published>2026-08-27T10:00:00Z</published>
        <location>Mumbai, India</location>
        <content type="html">&lt;p&gt;Human resources and recruitment operations.&lt;/p&gt;</content>
      </entry>
    </feed>
    """

    with patch("edgedash.sources.hasjob.get_text", return_value=mock_xml):
        source = HasjobSource()
        rows = source.fetch(mock_config)

        assert len(rows) == 1
        row = rows[0]
        for key in NORMALISED_KEYS:
            assert key in row

        assert row["source"] == "hasjob"
        assert row["external_id"] == "abc12"
        assert row["title"] == "Lead Python & SQL Data Engineer"
        assert row["company"] == "Dataflow"
        assert "Bengaluru" in row["location"]
        assert "Python" in row["description"]


def test_hasjob_source_http_error(mock_config):
    with patch("edgedash.sources.hasjob.get_text", side_effect=SourceError("Network timeout")):
        source = HasjobSource()
        rows = source.fetch(mock_config)
        assert rows == []

