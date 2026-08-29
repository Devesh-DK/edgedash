"""
MockFetcher — returns 12 realistic fake job listings without any network calls.

Deduplication contract
----------------------
Listings 0–3 carry FIXED source+url pairs so their stable_id never changes.
On the first run all 12 are new.  On every subsequent run those 4 are ignored
by INSERT OR IGNORE, and upsert_listings returns 8 (the 8 varying ones).
This makes deduplication directly observable in the console output.
"""

from __future__ import annotations

from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions

# ---------------------------------------------------------------------------
# Stable fixtures — same id on every run (dedup proof)
# ---------------------------------------------------------------------------
_STABLE: list[dict] = [
    {
        "source": "linkedin",
        "url": "https://linkedin.com/jobs/view/1000001",
        "title": "Data Analyst",
        "company": "Flipkart",
        "location": "Bengaluru, Karnataka",
        "description": (
            "Join the analytics team at Flipkart. "
            "You will build SQL pipelines, maintain Tableau dashboards, "
            "and partner with product teams on A/B test analysis. "
            "Required: Python, SQL, pandas, Excel."
        ),
        "posted_at": "2026-08-05T09:00:00+00:00",
    },
    {
        "source": "linkedin",
        "url": "https://linkedin.com/jobs/view/1000002",
        "title": "Senior Data Analyst",
        "company": "Swiggy",
        "location": "Bengaluru, Karnataka",
        "description": (
            "Own end-to-end reporting for the growth org at Swiggy. "
            "Work with large-scale event data using Spark and SQL. "
            "5+ years experience. Skills: Python, SQL, Spark, Power BI."
        ),
        "posted_at": "2026-08-04T11:30:00+00:00",
    },
    {
        "source": "naukri",
        "url": "https://naukri.com/job-listings/data-analyst-ola-bengaluru-3001",
        "title": "Data Analyst – Mobility",
        "company": "Ola",
        "location": "Bengaluru, Karnataka",
        "description": (
            "Analyse ride and driver metrics at Ola. "
            "Build ETL pipelines in Python, create dashboards in Tableau, "
            "and present insights to senior leadership. "
            "Must have: SQL, Python, data visualisation."
        ),
        "posted_at": "2026-08-06T08:00:00+00:00",
    },
    {
        "source": "naukri",
        "url": "https://naukri.com/job-listings/data-analyst-meesho-bengaluru-3002",
        "title": "Junior Data Analyst",
        "company": "Meesho",
        "location": "Bengaluru, Karnataka",
        "description": (
            "Entry-level analyst role at Meesho for the supply-chain team. "
            "Write SQL queries, clean data with pandas, and build Excel reports. "
            "0–2 years experience. Skills: SQL, Excel, Python basics."
        ),
        "posted_at": "2026-08-07T10:00:00+00:00",
    },
]

# ---------------------------------------------------------------------------
# Variable fixtures — url contains a timestamp fragment so id shifts each run
# (in production this simulates new listings appearing on the board)
# ---------------------------------------------------------------------------
def _variable_listings(fetched_at: str) -> list[dict]:
    ts = fetched_at[:10].replace("-", "")   # e.g. "20260811"
    return [
        {
            "source": "linkedin",
            "url": f"https://linkedin.com/jobs/view/var-{ts}-001",
            "title": "Business Analyst",
            "company": "PhonePe",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Drive product analytics at PhonePe. "
                "Skills: SQL, Python, Tableau, stakeholder communication."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "linkedin",
            "url": f"https://linkedin.com/jobs/view/var-{ts}-002",
            "title": "Data Analyst – Risk",
            "company": "Razorpay",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Detect fraud patterns using statistical models. "
                "Skills: Python, SQL, NumPy, scikit-learn basics."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "naukri",
            "url": f"https://naukri.com/job-listings/da-wipro-{ts}",
            "title": "Data Analyst – Consulting",
            "company": "Wipro",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Client-facing analyst for Wipro's analytics practice. "
                "Skills: SQL, Excel, Power BI, PowerPoint."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "naukri",
            "url": f"https://naukri.com/job-listings/da-infosys-{ts}",
            "title": "Associate Data Analyst",
            "company": "Infosys",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Support data engineering team with pipeline monitoring "
                "and ad-hoc reporting. Skills: SQL, Python, ETL tools."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "indeed",
            "url": f"https://indeed.com/viewjob?jk=var{ts}005",
            "title": "Product Data Analyst",
            "company": "Dunzo",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Own the metrics for Dunzo's quick-commerce product. "
                "Skills: SQL, Python, Google Analytics, Mixpanel."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "indeed",
            "url": f"https://indeed.com/viewjob?jk=var{ts}006",
            "title": "Marketing Analyst",
            "company": "Urban Company",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Analyse campaign performance and LTV at Urban Company. "
                "Skills: SQL, Excel, Google Data Studio, Python."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "linkedin",
            "url": f"https://linkedin.com/jobs/view/var-{ts}-007",
            "title": "Data Analyst – Supply Chain",
            "company": "BigBasket",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Optimise inventory and demand forecasting at BigBasket. "
                "Skills: Python, SQL, pandas, forecasting models."
            ),
            "posted_at": fetched_at,
        },
        {
            "source": "naukri",
            "url": f"https://naukri.com/job-listings/da-zepto-{ts}",
            "title": "Senior Business Analyst",
            "company": "Zepto",
            "location": "Bengaluru, Karnataka",
            "description": (
                "Lead data initiatives across dark-store operations at Zepto. "
                "Skills: SQL, Tableau, Python, 4+ years experience."
            ),
            "posted_at": fetched_at,
        },
    ]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class MockFetcher:
    name: str = "MockFetcher"

    def run(self, config: Config, stop_conditions: StopConditions) -> AgentResult:
        fetched_at = datetime.now(timezone.utc).isoformat()

        rows: list[dict] = []
        for raw in _STABLE + _variable_listings(fetched_at):
            rows.append({
                "id": storage.stable_id(raw["source"], raw["url"]),
                "title": raw["title"],
                "company": raw["company"],
                "location": raw.get("location", config.target_city),
                "url": raw["url"],
                "description": raw["description"],
                "source": raw["source"],
                "posted_at": raw.get("posted_at"),
                "fetched_at": fetched_at,
                "fit_score": None,
                "fit_reason": None,
            })

        new_count = storage.upsert_listings(rows)

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=(
                f"Offered {len(rows)} listings to storage. "
                f"{new_count} were new (4 stable fixtures deduped on repeat runs)."
            ),
        )
