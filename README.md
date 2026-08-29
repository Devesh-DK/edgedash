# EdgeDash

EdgeDash is an autonomous AI career intelligence agent. On a schedule it fetches
live job listings from multiple sources, scores each one for fit against your
skills profile, identifies the skill gaps appearing most frequently across the
board, verifies its own output, and publishes everything to a Streamlit dashboard
— so your job search runs in the background while you work.

---

## Architecture

```
Trigger (scheduled)
  └── Orchestrator
        ├── Fetcher        pulls raw listings into storage
        ├── Scorer         writes fit_score + fit_reason per listing
        └── GapAnalyzer    aggregates missing skills into skill_gaps table
             └── Verifier
                  └── Storage (SQLite → Postgres in week 4)
                        └── Dashboard (read-only)
```

The Orchestrator reads state and delegates. It never fetches or scores directly.
Each sub-agent has one goal and one stop condition.
The Dashboard only reads from Storage; it never writes.

---

## Current status

### Week 1 — foundation (complete)
- [x] `edgedash/config.py` — `Config` dataclass loaded from `config.yaml`
- [x] `edgedash/storage.py` — sole module allowed to touch SQLite; thin public interface
- [x] `edgedash/agents/base.py` — `Agent` protocol and `AgentResult` dataclass
- [x] `edgedash/agents/mock_fetcher.py` — **temporary** stand-in; 12 fake listings, 4 stable fixtures to prove dedup
- [x] `edgedash/orchestrator.py` — registry-based cycle runner with state read, plan, execute, log, summary
- [x] `run_cycle.py` — entry point
- [x] `cycle_log` table records every agent run

### Week 2 — real data (coming)
- [ ] `edgedash/agents/fetcher.py` — replace MockFetcher; live job-board API calls
- [ ] `edgedash/agents/scorer.py` — LLM-backed fit scoring against profile
- [ ] Environment variable wiring for API keys

### Week 3 — intelligence layer (coming)
- [ ] `edgedash/agents/gap_analyzer.py` — frequency-ranked skill gap extraction
- [ ] `edgedash/verifier.py` — sanity-checks agent output before it lands in storage
- [ ] Streamlit dashboard (read-only)

### Week 4 — production (coming)
- [ ] Swap SQLite for hosted Postgres (one-file change in `storage.py`)
- [ ] Scheduled trigger (cron / cloud scheduler)
- [ ] Alerting on cycle failures

---

## Setup

**Python 3.11 or later is required.**

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd edgedash

# 2. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS / Linux

# 3. Install dependencies
pip install pyyaml
# (more will be added as real agents are built)
```

### Configure your profile

Edit `config.yaml` at the repo root before running:

```yaml
target_role: "Data Analyst"
target_city: "Bengaluru"
keywords:
  - "SQL"
  - "Python"
my_skills:
  - "Python"
  - "SQL"
  - "pandas"
experience_years: 3
db_path: "edgedash.db"
min_fit_score: 60
```

All user-specific values live here. Nothing is hardcoded in the source.

### Run a cycle

```bash
python run_cycle.py
```

The first run initialises the database and inserts all listings.
Subsequent runs insert only listings whose `source + url` hash is new —
deduplication is reported in the console output.

---

## Design decisions

**Storage is isolated behind one module.**
`edgedash/storage.py` is the only file that imports a database driver. Every
other module calls its public functions. When SQLite is swapped for Postgres in
week 4, only `storage.py` changes — no other file needs to know.

**Listing IDs are stable hashes of source + URL.**
The same job posted on the same board always produces the same ID, regardless of
when it was fetched. `INSERT OR IGNORE` on the primary key then makes dedup a
database primitive rather than application logic, and the new-row count is
directly observable.

**The Orchestrator delegates instead of doing the work itself.**
Keeping fetch, score, and gap-analysis in separate agents means each can be
tested, replaced, or skipped independently. The Orchestrator's only job is to
read state, decide what runs, and record what happened — a separation that also
makes the registry swap (MockFetcher → real Fetcher) a one-line change.
