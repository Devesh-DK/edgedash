"""
edgedash/query/ask.py — two-call natural-language query pipeline (rules 42-45).

Public API
----------
    ask(question, config) -> Answer

The model appears exactly twice per question (rule 42):
    1. ROUTE  — pick a tool and its parameters from the fixed registry.
    2. PHRASE — turn the returned rows into 2-3 sentences of prose.

It never touches the database in either call (rule 42).
The phrasing call may only use numbers present in the rows (rule 43).
If no tool matches, the model is never called for phrasing (rule 45).

No SQL generation. No text-to-SQL. No dynamic getattr on model output.
"""

from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.llm import LLMError, complete_json
from edgedash.query.tools import TOOLS, ToolResult, call as tool_call

# ---------------------------------------------------------------------------
# Input guards & injection patterns (rule 45 abuse guards)
# ---------------------------------------------------------------------------

_MAX_QUESTION_LENGTH = 300
_INJECTION_PATTERNS = (
    "ignore previous",
    "system prompt",
    "you are now",
)


def sanitize_question(text: str) -> str:
    """Strip control characters and excessive surrounding whitespace."""
    chars = [ch for ch in text if not unicodedata.category(ch).startswith("C") or ch in "\t\n"]
    return "".join(chars).strip()


# ---------------------------------------------------------------------------
# Answer dataclass
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    """The complete response to one natural-language question."""
    text:       str             # prose produced by the phrase call (or fixed message)
    rows:       list[dict]      = field(default_factory=list)
    tool_used:  str | None      = None   # None when no tool matched
    params:     dict[str, Any]  = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Routing prompt (rule 42 — the text the router model sees)
# ---------------------------------------------------------------------------

def _build_route_prompt(question: str) -> str:
    """Return the full routing prompt shown to the model.

    The model sees:
      - The question.
      - Each tool's name, description, and parameter specs.
      - Nothing else: no schema, no SQL, no table names, no column names.

    The prompt instructs the model explicitly (rule 45):
      - Return null if nothing matches. Do NOT pick the closest tool.
    """
    # Build the tool catalogue — name, description, and each parameter.
    catalogue_lines: list[str] = []
    for spec in TOOLS.values():
        param_parts: list[str] = []
        for p in spec.parameters:
            default_note = f", default {p.default}" if p.default is not None else ""
            param_parts.append(f"    - {p.name} ({p.type}{default_note}): {p.description}")
        params_block = "\n".join(param_parts) if param_parts else "    (no parameters)"
        catalogue_lines.append(
            f'TOOL: "{spec.name}"\n'
            f"  WHEN TO USE: {spec.description}\n"
            f"  PARAMETERS:\n{params_block}"
        )
    catalogue = "\n\n".join(catalogue_lines)

    # -----------------------------------------------------------------------
    # THE ROUTING PROMPT
    # This is the exact text sent to the model. Keep it precise and honest
    # about the null case — the model must not invent a match.
    # -----------------------------------------------------------------------
    return f"""\
You are a query router. Your only job is to read a question and decide
which tool from the list below should answer it, together with the
parameter values to pass.

QUESTION:
{question}

AVAILABLE TOOLS:
{catalogue}

INSTRUCTIONS:
- Read the WHEN TO USE description for each tool carefully.
- Choose the ONE tool whose description most directly matches the question.
- Fill in the parameter values that best match what the question is asking.
  Use the default value when the question does not specify.
- If no tool matches the question — even approximately — set "tool" to null.
  Do NOT pick the closest tool when you are uncertain. Return null.
- Confidence: set "high" if the match is unambiguous; "low" if you had to
  interpret the question generously. Set null in "tool" rather than "low"
  confidence when genuinely unsure.

Respond with a JSON object that has exactly these keys:
  "tool":       the tool name as a string, or null if nothing matches
  "params":     an object with the parameter values (empty object if tool is null)
  "confidence": "high" or "low"\
"""


# ---------------------------------------------------------------------------
# Phrasing prompt (rule 43 — only numbers present in the rows)
# ---------------------------------------------------------------------------

def _build_phrase_prompt(
    question: str,
    rows: list[dict],
    summary: str,
) -> str:
    """Return the phrasing prompt shown to the model.

    The model is given:
      - The original question.
      - The tool's summary sentence (scope: "47 listings from the last 7 days").
      - The exact rows that were returned — nothing else.

    Rule 43 constraints are stated explicitly in the prompt:
      - Use only numbers present in these rows.
      - Do not estimate, extrapolate, or add outside context.
      - If rows are empty, say the data does not contain an answer.
    """
    import json as _json

    rows_text = _json.dumps(rows, indent=2) if rows else "[]"

    return f"""\
You are answering a question about job market data. Write 2-3 clear sentences.

QUESTION:
{question}

SCOPE (what the query looked at):
{summary}

DATA ROWS (the only source of truth — do not use any other information):
{rows_text}

STRICT RULES:
- Use ONLY the numbers that appear in the data rows above.
- Do NOT estimate, extrapolate, infer, or add any context from outside these rows.
- Do NOT mention skills, companies, counts, or scores that are not in the rows.
- If the data rows are empty (shown as []), your answer must say:
  "The data does not contain an answer to this question."
- Write in plain English. No bullet points. No headers. 2-3 sentences maximum.

Respond with a JSON object that has exactly one key:
  "text": your 2-3 sentence answer as a plain string\
"""


# ---------------------------------------------------------------------------
# Schema definitions for the two model calls
# ---------------------------------------------------------------------------

_ROUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "tool":       {"type": ["string", "null"]},
        "params":     {"type": "object"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["tool", "params", "confidence"],
    "additionalProperties": False,
}

_PHRASE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1},
    },
    "required": ["text"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Null-tool response (rule 45 — fixed message, no model call)
# ---------------------------------------------------------------------------

def _no_tool_answer(question: str) -> Answer:
    """Build the fixed Answer when the router returned tool=null (rule 45).

    Lists available tools in plain English — no model call for phrasing.
    """
    tool_list = "\n".join(
        f"  • {spec.name}: {spec.description.split('.')[0]}."
        for spec in TOOLS.values()
    )
    text = (
        f"This question can't be answered with the available query tools.\n\n"
        f"Questions I can answer:\n{tool_list}"
    )
    return Answer(text=text, rows=[], tool_used=None, params={})


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def ask(question: str, config: Config) -> Answer:
    """Run the two-call pipeline and return an Answer.

    Call 1 (ROUTE): model picks a tool + params from the fixed registry.
    Call 2 (PHRASE): model turns the returned rows into prose.

    If the router returns tool=null, no second call is made and a fixed
    "can't answer" message listing available tools is returned (rule 45).

    Every question is logged to query_log regardless of outcome (rule 5).

    Args:
        question: The raw question string from the user.
        config:   The loaded Config instance (LLM provider, model, rate limits).

    Returns:
        Answer with .text, .rows, .tool_used, .params.

    Raises:
        LLMError: If the routing or phrasing call fails after retries.
                  The caller decides whether to surface this to the user.
    """
    t_start = time.monotonic()
    asked_at = storage.now_utc()

    # ── INPUT GUARDS (rule 45 abuse guards) ───────────────────────────────────
    # 1. Strip control characters
    cleaned = sanitize_question(question)

    # 2. Reject empty or whitespace-only input
    if not cleaned:
        return Answer(
            text="Please enter a question.",
            rows=[],
            tool_used=None,
            params={"reason": "rejected: empty input"},
        )

    # 3. Reject questions over 300 characters
    if len(cleaned) > _MAX_QUESTION_LENGTH:
        return Answer(
            text="Question is too long (maximum 300 characters).",
            rows=[],
            tool_used=None,
            params={"reason": "rejected: question exceeds 300 characters"},
        )

    # 4. Detect obvious instruction-injection patterns
    q_lower = cleaned.lower()
    if any(pat in q_lower for pat in _INJECTION_PATTERNS):
        answer = _no_tool_answer(cleaned)
        return answer

    # 5. Global daily cap check
    try:
        if storage.count_queries_today() >= config.daily_ask_cap:
            return Answer(
                text="The daily question limit has been reached. Please check back tomorrow.",
                rows=[],
                tool_used=None,
                params={"reason": "rejected: daily cap reached"},
            )
    except Exception:
        pass

    # ── CALL 1: ROUTE ────────────────────────────────────────────────────────
    route_prompt = _build_route_prompt(cleaned)
    route_result = complete_json(
        route_prompt,
        _ROUTE_SCHEMA,
        config=config,
        max_retries=1,
    )

    tool_name: str | None = route_result.get("tool")
    raw_params: dict      = route_result.get("params") or {}

    # Hard validation: tool name must be in registry or null.
    # Anything else is a model error, not a fallback (rule 45).
    if tool_name is not None and tool_name not in TOOLS:
        raise LLMError(
            f"Router returned unknown tool name '{tool_name}'. "
            f"Valid names: {sorted(TOOLS)}. "
            "This is a model error — the router must only return names from "
            "the catalogue it was given, or null."
        )

    # ── NULL TOOL: no second call (rule 45) ──────────────────────────────────
    if tool_name is None:
        answer = _no_tool_answer(cleaned)
        return answer

    # ── EXECUTE: validated, clamped tool call (rule 41) ──────────────────────
    # tool_call() validates and clamps every parameter before touching storage.
    # Never eval, never getattr on a model-supplied string outside this lookup.
    result: ToolResult = tool_call(tool_name, **raw_params)

    # ── CALL 2: PHRASE ───────────────────────────────────────────────────────
    phrase_prompt = _build_phrase_prompt(cleaned, result.rows, result.summary)
    phrase_result = complete_json(
        phrase_prompt,
        _PHRASE_SCHEMA,
        config=config,
        max_retries=1,
    )

    answer = Answer(
        text=phrase_result["text"],
        rows=result.rows,
        tool_used=tool_name,
        params=raw_params,
    )

    return answer


