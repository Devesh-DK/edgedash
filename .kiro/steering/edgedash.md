# EdgeDash — Project Steering Rules

EdgeDash is an autonomous AI career intelligence agent. It runs on a schedule, fetches live job listings, scores them against a user profile, surfaces skill gaps, verifies its own output, and publishes results through a read-only Streamlit dashboard.

---

## Architecture

Follow this pipeline exactly. Do not deviate without telling the user first.

```
Trigger (scheduled)
  -> Orchestrator
      -> Fetcher        (sub-agent)
      -> Scorer         (sub-agent)
      -> GapAnalyzer    (sub-agent)
  -> Verifier
  -> Storage
  -> Dashboard (read-only)
```

- The **Orchestrator** reads state and delegates work. It never fetches data or scores jobs directly.
- Each **sub-agent** has exactly one goal and one stop condition.
- The **Dashboard** is read-only — it queries Storage and renders; it never writes.

---

## Hard Rules

### 1 — Python version and dependencies
- Target Python 3.11+.
- Reach for the standard library first.
- Add a third-party dependency only when it genuinely saves real work. State the reason before adding it.

### 2 — Storage access
- ALL database access goes through a single `storage` module with a thin interface.
- No other module may import `sqlite3` (or any DB driver) directly.
- The goal: swapping SQLite for hosted Postgres in week 4 must be a **one-file change**.

### 3 — No hardcoded user-specific values
- Role, city, keywords, and skills profile must never appear as literals in code.
- Everything user-specific lives in `config` (e.g., `config.py` or a config YAML loaded in one place).

### 4 — No secrets in code
- Credentials, API keys, and tokens use environment variables only.
- Environment variables are loaded in exactly one place in the codebase.

### 5 — Cycle logging
- Every agent run writes a row to the `cycle_log` table containing:
  - which agent ran
  - timestamp
  - number of records touched
  - pass/fail status
  - retry reason (if any)

### 6 — Fail loudly
- No bare `except: pass` or silent swallowing of errors.
- If something goes wrong, raise or log visibly so the operator knows immediately.

### 7 — Type hints and docstrings
- Every function signature must have type hints (parameters and return type).
- Add docstrings only where the intent is not obvious from the name alone.

### 8 — File length
- Keep files under ~150 lines.
- Split a module before it becomes a problem, not after.

---

## Style

- Write small, testable functions.
- Plain, readable Python over clever Python.
- When asked to build one module, build that module — do not scaffold the whole app.

---

## Network & Sources

### 9 — Source interface
- Every external job board lives behind a `Source` class with a uniform interface.
- The Fetcher never contains source-specific parsing logic.
- Adding a new source must never require editing the Fetcher — only a new `Source` class and a registry entry.

### 10 — Normalised output contract
- Every `Source` returns a list of dicts with **exactly** these keys:
  `source`, `external_id`, `title`, `company`, `location`, `url`, `description`, `posted_at`, `raw`.
- Missing values are `None`. Never empty string, never `"N/A"`.

### 11 — All network calls go through one helper
- A single shared helper handles all HTTP requests: 10 s timeout (default), 2 retry attempts with exponential backoff, and a real `User-Agent` header.
- No bare `requests.get()` anywhere else in the codebase.

### 12 — Source failures are isolated
- A failing source must never kill the cycle.
- Catch exceptions per-source, log the failure to `cycle_log` with `status="failed"`, and continue to the next source.
- One dead job board must not stop the others.

### 13 — Secrets for sources
- Source credentials come from environment variables loaded from a `.env` file.
- `.env` is gitignored. No literal keys in code, no keys in `config.yaml`.
- If a required key is missing, that source skips itself with a clear log line — it does not crash the cycle.

### 14 — Respect the source
- Rate limit to at most 1 request per second per source.
- Set a descriptive `User-Agent` header on every request.
- Honour any documented page limits from the source's API or terms of service.

---

## Intelligence & Scoring

### 15 — One LLM gateway module
- All LLM calls go through `edgedash/llm.py`, which exposes exactly one public function.
- The provider and model name come from `config`, never hardcoded.
- Rate-limit to stay inside a free tier: default 1 request per second, max 15 per minute.
- No other file may import an LLM SDK directly.

### 16 — Model extracts facts; Python scores
- NEVER ask a model for a final score, ranking, or numeric rating.
- The model extracts structured facts only (e.g. required skills, experience level, keywords present).
- All scoring arithmetic is deterministic Python in ONE function.
- The model never sees the scoring weights.

### 17 — Validate every model response
- Every model response is validated against an explicit schema before use.
- A response that fails validation is retried once, then logged as a failure for that listing only.
- A validation failure must not crash the cycle or stop the remaining listings from being processed.
- Never call `json.loads` on raw model text without a validation and repair path.

### 18 — Idempotent scoring with description-hash cache
- Scoring is idempotent. Never re-score a listing that already has a score.
- Select only listings `WHERE score IS NULL`.
- Cache extraction results keyed on a hash of the job description so the same text is never sent to the model twice.

### 19 — Code-generated score reasons
- Every score carries a human-readable reason generated from the score components by our code — never free text written by the model.

### 20 — Log score distribution every run
- Log the score distribution (count, min, max, mean, spread) to `cycle_log` on every scoring run.
- A run where all scores fall within a 10-point range is a suspect run and must be flagged as such in the log.

### 21 — Configurable batch size cap
- Cap the number of listings scored per cycle at a configurable batch size (default 25).
- This makes a cost or rate-limit blowup structurally impossible.

---

## Aggregate Analysis

### 22 — Aggregates are deterministic SQL and Python
- No LLM call may produce, adjust, or rank an aggregate number.
- A model may only SUGGEST canonical groupings for a human to approve.

### 23 — Skill names are canonicalised through an explicit alias map
- Skill names are normalised through an alias map in `config.yaml` that the user owns and can read.
- Never auto-merge skill names by model judgement or string similarity alone.

### 24 — Gap ranking is weighted by listing fit score
- A gap found in a listing scored 20 is worth far less than the same gap in a listing scored 85.
- Never rank gaps by raw frequency alone.

### 25 — Every gap report run writes a timestamped snapshot
- Never overwrite the previous report.
- Trend over time is a first-class output, not an afterthought.

### 26 — Every aggregate number must be traceable to its source rows
- Any reported gap must be able to list the specific listing IDs it was computed from.
- No number appears in the dashboard that cannot be drilled into.

### 27 — Always report sample size alongside every aggregate
- A gap computed from 3 listings and a gap computed from 90 listings must never be presented as equally reliable.

---

## Orchestration

### 28 — State-driven dispatch, not fixed sequence
- The Orchestrator reads system state and decides which agents to run.
- It never runs a fixed sequence. Skipping an agent because there is no work for it is a SUCCESSFUL outcome, not a failure.

### 29 — Every delegation carries explicit limits
- Every delegation carries an explicit goal and an explicit stop condition (max items, max duration).
- A sub-agent never decides its own limits — the Orchestrator sets them.

### 30 — Orchestrator does not do agent work
- The Orchestrator never does an agent's work. It reads state, delegates, collects results, and logs.
- No fetching, scoring, or analysis logic belongs in the Orchestrator.

### 31 — Log the plan before executing it
- The Orchestrator prints and logs its PLAN before executing it: which agents will run, which are skipped, and the state value that caused each decision.

### 32 — One sub-agent failing does not stop the cycle
- Log the failure, continue with the remaining plan, and mark the cycle partial.

### 33 — One summary row per cycle
- Every cycle writes exactly one summary row: what ran, what was skipped, why, duration per agent, and the outcome.

---

## Verification

### 34 — Verifier verdict only; Orchestrator decides
- The Verifier judges output plausibility and NEVER repairs, rewrites, or adjusts data.
- It returns a verdict and a reason. The Orchestrator decides what to do about a failure.

### 35 — Verification checks plausibility, not correctness
- There is no ground truth for a fit score.
- Checks assert properties of the output distribution and shape, not the accuracy of any single value.

### 36 — At most one retry; then "degraded"
- A failed verification triggers at most ONE retry of the failing agent with adjusted context.
- After that the cycle is marked "degraded" and stops.
- Never retry in an unbounded loop.

### 37 — Log every verdict with the failing check and observed value
- Every verdict is logged to `cycle_log` with the check that failed and the observed value that failed it.
- Never log just "failed" — the specific check name and observed value are required.

### 38 — Failed cycles must never overwrite last known-good data
- Only cycles with a passing verdict may be read by the dashboard.
- A failed cycle must never overwrite the last known-good data.
- Stale verified data always beats fresh unverified data.

### 39 — Verification thresholds live in config.yaml
- Verification thresholds live in `config.yaml`, not in code.
- Every threshold must have a comment saying what failure it is designed to catch.

---

## Natural Language Queries

### 40 — No text-to-SQL, ever
- NEVER generate SQL from a model. No text-to-SQL in any form.
- The model selects from a fixed registry of parameterised query functions that were written by the developer. It never composes a query.

### 41 — Every query tool is read-only and parameterised
- Every query tool is read-only, parameterised, and accepts typed parameters.
- Parameters are validated and clamped to a safe range before execution.
- A model-supplied parameter is untrusted input — treat it accordingly.

### 42 — The model appears exactly twice per question
- Once to ROUTE: pick a tool and its parameters from the fixed registry.
- Once to PHRASE: turn the returned rows into prose.
- It never touches the database in either call.

### 43 — The phrasing call is strictly bound to its rows
- The phrasing call may use ONLY the numbers present in the rows it was given.
- It must not estimate, extrapolate, add outside context, or infer a value that is not in the data.
- If the rows are empty it must say so plainly.

### 44 — Every answer displays the underlying rows
- No prose answer appears without the data that produced it.
- The underlying rows are shown alongside every answer.

### 45 — No tool match means no answer
- If no tool matches the question, say so and list what CAN be asked.
- Never guess at the closest tool and never answer from general knowledge.

### 46 — Query tools read from the last passing cycle only
- Query tools read from the last passing cycle only, per rule 38.
- No query may surface data from a failed or degraded cycle.

---

## DEPLOYMENT

### 47 — Never rely on the local filesystem for anything that must survive a restart
- Hosting filesystems are ephemeral. All persistent state is in the hosted database.

### 48 — Every secret comes from an environment variable read in one place
- No secret is ever committed, printed, logged, or shown in an error message or traceback.

### 49 — Separate processes
- The scheduled job and the dashboard are separate processes that share only the database.
- The dashboard never runs a cycle; the scheduler never serves a page.

### 50 — Resilient startup
- The deployed app must start and render even when the database is empty, unreachable, or mid-migration.
- It shows a clear status message instead of a stack trace. A stranger must never see a traceback.

### 51 — Idempotent and bounded scheduler
- The scheduled job is idempotent and safe to run twice.
- It must have a hard timeout and stay inside free-tier limits.
