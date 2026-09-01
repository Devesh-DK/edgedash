"""
edgedash/llm.py — the single gateway to any language model (steering rule 15).

Public API
----------
    complete_json(prompt, schema, *, max_retries=1) -> dict

    Sends `prompt` to the configured provider, parses the response as JSON,
    validates it against `schema`, and returns the validated dict.
    Retries once on parse/validation failure, then raises LLMError.

Providers
---------
    "gemini"  — Google Generative AI (GEMINI_API_KEY env var)
    "ollama"  — local Ollama HTTP endpoint (no key required)

    Adding a third provider means adding one entry to _PROVIDERS only.
    complete_json is never edited for provider changes.

Rate limiting (steering rule 15)
---------------------------------
    - Minimum gap between calls: config.llm_requests_per_second  (default 1 s)
    - Rolling window cap:        config.llm_requests_per_minute   (default 15)
    - On 429 / quota error: exponential backoff, 3 attempts, then raise.

Validation (steering rule 17)
------------------------------
    jsonschema is used because it validates against an explicit schema dict in
    one call and produces detailed error messages useful for the repair prompt —
    genuine saved work over a hand-rolled validator (steering rule 1).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

# Ensure environment variables from .env are loaded into os.environ
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.is_file():
    load_dotenv(_env_path)
else:
    load_dotenv()

from edgedash.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class LLMError(RuntimeError):
    """Raised when a model call fails after all retries, or is misconfigured."""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Strip markdown fences and surrounding prose; return the JSON fragment.

    Strategy:
    1. If a ```json … ``` or ``` … ``` fence is present, take its contents.
    2. Otherwise find the first '{' or '[' and last matching '}' or ']'.
    3. Fall back to the raw text (parse error will surface on json.loads).
    """
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()

    # Find the outermost JSON object or array
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            return text[start : end + 1]

    return text.strip()


def _parse_and_validate(text: str, schema: dict) -> dict:
    """Extract JSON from model text, parse it, and validate against schema.

    Raises:
        json.JSONDecodeError: if the extracted text is not valid JSON.
        jsonschema.ValidationError: if the parsed object fails schema validation.
    """
    import jsonschema  # imported here so the error is local and descriptive

    fragment = _extract_json(text)
    data = json.loads(fragment)
    jsonschema.validate(instance=data, schema=schema)
    return data  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Enforces min-gap and rolling-window caps; sleeps to stay within limits."""

    def __init__(self, min_gap_s: float, max_per_minute: int) -> None:
        self._min_gap = min_gap_s
        self._max_per_minute = max_per_minute
        self._call_times: deque[float] = deque()
        self._last_call: float = 0.0

    def wait(self) -> None:
        """Block until both rate constraints are satisfied, then record the call."""
        now = time.monotonic()

        # 1 — minimum gap between consecutive calls
        gap_needed = self._min_gap - (now - self._last_call)
        if gap_needed > 0:
            time.sleep(gap_needed)
            now = time.monotonic()

        # 2 — rolling 60-second window cap
        window_start = now - 60.0
        while self._call_times and self._call_times[0] < window_start:
            self._call_times.popleft()

        if len(self._call_times) >= self._max_per_minute:
            oldest = self._call_times[0]
            sleep_until = oldest + 60.0
            sleep_for = sleep_until - now
            if sleep_for > 0:
                logger.info("LLM rate limit: sleeping %.1f s (rolling window)", sleep_for)
                time.sleep(sleep_for)
            now = time.monotonic()

        self._last_call = now
        self._call_times.append(now)


# Module-level limiter; re-initialised by _get_limiter() on first use or
# when config changes. Each process has its own limiter (single-cycle design).
_limiter: _RateLimiter | None = None


def _get_limiter(config: Config) -> _RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = _RateLimiter(
            min_gap_s=float(config.llm_requests_per_second),
            max_per_minute=config.llm_requests_per_minute,
        )
    return _limiter


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------
# Each provider is a callable:  (prompt: str, model: str) -> str
# It must raise LLMError on quota/auth errors, RuntimeError on transient ones.

def _call_gemini(prompt: str, model: str) -> str:
    """Call Google Generative AI via generate_content and return response text.

    Uses client.models.generate_content with the model name from config.
    The Interactions API path is intentionally removed -- it hardcodes
    gemini-3.6-flash which has a 20 RPD free quota that exhausts immediately.
    The configured model (e.g. gemini-3.5-flash-lite) has a much higher quota.

    Quota/rate errors are wrapped as LLMError so _call_with_backoff can
    catch them and respect the server-supplied retryDelay.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        _env = Path(__file__).resolve().parent.parent / ".env"
        if _env.is_file():
            load_dotenv(_env, override=True)
        else:
            load_dotenv(override=True)
        api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY is not set. "
            "Add it to your .env file (see .env.example)."
        )

    # -- New SDK: google-genai -----------------------------------------------
    try:
        from google import genai as new_genai  # type: ignore[import]
        client = new_genai.Client(api_key=api_key)
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            return response.text or ""
        except Exception as exc:
            msg = str(exc)
            if any(k in msg.lower() for k in ("429", "quota", "resource_exhausted",
                                               "too_many_requests")):
                raise LLMError(f"Gemini quota/rate error: {exc}") from exc
            raise LLMError(f"Gemini call failed: {exc}") from exc
    except ImportError:
        pass  # new SDK not installed -- try old SDK below

    # -- Old SDK: google-generativeai ----------------------------------------
    try:
        import google.generativeai as old_genai  # type: ignore[import]
    except ImportError as exc:
        raise LLMError(
            "No Gemini SDK is installed. Run one of:\n"
            "  pip install google-genai            (recommended, new SDK)\n"
            "  pip install google-generativeai     (legacy SDK)"
        ) from exc

    old_genai.configure(api_key=api_key)
    client_old = old_genai.GenerativeModel(model)
    try:
        response = client_old.generate_content(prompt)
        return response.text or ""
    except Exception as exc:
        msg = str(exc).lower()
        if "quota" in msg or "429" in msg or "resource_exhausted" in msg:
            raise LLMError(f"Gemini quota/rate error: {exc}") from exc
        raise LLMError(f"Gemini call failed: {exc}") from exc


def _call_ollama(prompt: str, model: str) -> str:
    """Call a local Ollama instance via its HTTP API and return the response text."""
    import urllib.request

    url = "http://localhost:11434/api/generate"
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:
        raise LLMError(
            f"Ollama call failed (is Ollama running on localhost:11434?): {exc}"
        ) from exc

    return body.get("response", "")


# Registry: provider name -> callable.
# Adding a third provider = one new function + one dict entry. complete_json
# is never touched.
_PROVIDERS: dict[str, Callable[[str, str], str]] = {
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def complete_json(
    prompt: str,
    schema: dict,
    *,
    config: Config,
    max_retries: int = 1,
) -> dict:
    """Send `prompt` to the configured LLM and return a validated JSON dict.

    The model is instructed to reply with JSON only. The response is stripped
    of markdown fences and prose, parsed, then validated against `schema`.

    On parse or validation failure the call is retried up to `max_retries`
    times with the exact validation error appended to the prompt.

    On 429 / quota errors, exponential backoff is applied for up to 3 attempts
    before raising LLMError.

    Args:
        prompt:      The user-facing prompt. Do NOT include JSON format
                     instructions — this function appends them.
        schema:      A JSON Schema dict the response must satisfy.
        config:      The loaded Config instance (provider/model/rate limits).
        max_retries: How many times to retry on validation failure (default 1).

    Returns:
        The parsed and validated response dict.

    Raises:
        LLMError: On misconfiguration, auth failure, quota exhaustion, or
                  persistent parse/validation failure.
    """
    provider_name = config.llm_provider
    model = config.llm_model

    call_fn = _PROVIDERS.get(provider_name)
    if call_fn is None:
        raise LLMError(
            f"Unknown LLM provider '{provider_name}'. "
            f"Supported providers: {sorted(_PROVIDERS)}. "
            "Update llm_provider in config.yaml."
        )

    limiter = _get_limiter(config)
    json_instruction = (
        "\n\nReply with a single JSON object only. "
        "No markdown fences, no prose, no explanation — raw JSON only."
    )
    full_prompt = prompt + json_instruction

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        # ── rate limit ────────────────────────────────────────────────────
        limiter.wait()

        # ── call with quota-error backoff ─────────────────────────────────
        raw_text = _call_with_backoff(call_fn, full_prompt, model)

        # ── parse + validate ──────────────────────────────────────────────
        try:
            return _parse_and_validate(raw_text, schema)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "LLM response failed validation (attempt %d/%d): %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )

            if attempt < max_retries:
                # Repair prompt: tell the model exactly what went wrong
                full_prompt = (
                    prompt
                    + f"\n\nYour previous response failed validation with: {exc}\n"
                    "Reply with a single JSON object only. "
                    "No markdown fences, no prose — raw JSON only."
                )

    raise LLMError(
        f"LLM response failed validation after {max_retries + 1} attempt(s). "
        f"Last error: {last_error}"
    ) from last_error


# ---------------------------------------------------------------------------
# Backoff helpers (used by complete_json above; defined here for locality)
# ---------------------------------------------------------------------------

def _parse_retry_delay(exc: Exception) -> float | None:
    """Extract the retryDelay from a Gemini 429 exception, if present.

    The SDK wraps the API error as an exception whose string representation
    contains the full JSON body. We look for:
      - 'retryDelay': '42s'   (inside details[].RetryInfo)
      - 'retry_delay': ...
      - plain 'retry in Xs' phrasing

    Returns the delay in seconds as a float, or None if not found.
    """
    text = str(exc)

    # Pattern 1: retryDelay key in either JSON double-quotes or Python repr single-quotes
    # Matches: "retryDelay": "42s"  OR  'retryDelay': '42s'
    m = re.search(r"""['"]retry[Dd]elay['"]\s*:\s*['"]?([\d.]+)s['"]?""", text)
    if m:
        return float(m.group(1))

    # Pattern 2: plain prose "retry in 42s" or "Please retry in 42.540120358s"
    m = re.search(r'retry\s+in\s+([\d.]+)s', text, re.IGNORECASE)
    if m:
        return float(m.group(1))

    return None


def _call_with_backoff(
    call_fn: Callable[[str, str], str],
    prompt: str,
    model: str,
    max_attempts: int = 3,
) -> str:
    """Call `call_fn` with exponential backoff on quota/rate errors.

    If the API response contains a retryDelay, that value is used instead
    of the default exponential delay — we never retry sooner than the API
    tells us to.
    """
    for attempt in range(max_attempts):
        try:
            return call_fn(prompt, model)
        except LLMError as exc:
            msg = str(exc).lower()
            is_quota = ("quota" in msg or "429" in msg or
                        "resource_exhausted" in msg or "too_many_requests" in msg)
            is_transient_net = any(k in msg for k in ("disconnected", "timeout", "timed out", "connection", "remoteprotocolerror", "500", "502", "503", "504", "unavailable", "server error", "internal server"))
            if (is_quota or is_transient_net) and attempt < max_attempts - 1:
                # Respect the server-supplied delay; fall back to exponential.
                api_delay = _parse_retry_delay(exc)
                wait = api_delay if api_delay is not None else float(2 ** attempt)
                logger.warning(
                    "LLM %s error (attempt %d/%d), retrying in %.1fs: %s",
                    "quota" if is_quota else "network",
                    attempt + 1,
                    max_attempts,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                raise
    # unreachable, but satisfies type checkers
    raise LLMError("Backoff exhausted")  # pragma: no cover


# ---------------------------------------------------------------------------
# CLI check:  python -m edgedash.llm --check
# ---------------------------------------------------------------------------

def _cli_check() -> None:
    """Send one trivial prompt and report provider, model, and success/failure."""
    from pathlib import Path
    from dotenv import load_dotenv
    from edgedash.config import load_config

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    cfg = load_config()

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    prompt = 'Reply with the JSON object {"ok": true}.'

    print(f"Provider : {cfg.llm_provider}")
    print(f"Model    : {cfg.llm_model}")
    print("Sending check prompt …")

    try:
        result = complete_json(prompt, schema, config=cfg)
        print(f"Response : {result}")
        print("Status   : OK")
    except LLMError as exc:
        print(f"Status   : FAILED — {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check")
